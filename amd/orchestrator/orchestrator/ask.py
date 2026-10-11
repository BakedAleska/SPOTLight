r"""The ask graph (POST /v1/ask): the System 2 model (gateway alias `system2`) with the read-only MCP tools, bounded.

    START -> agent -?-> tools -> agent ...            (model-driven, at most max_calls tool calls)
                   \-> fallback_retrieve -> answer_from_context -> finish   (no tool calls / tool calling failed)
                   \-> finish -> END

- Tools come from the MCP server's tools/list, filtered by ASK_TOOL_ALLOWLIST (search_*, get_*) and readOnlyHint.
  A call to anything else is refused but still counts toward the cap, so a model cannot loop on refusals.
- After the cap the model gets one more turn WITHOUT tools and must answer. Whole request within ASK_DEADLINE_S.
- llama-server needs `--jinja` for Qwen tool calling. Without it the first turn errors or returns plain text; then
  the orchestrator runs one retrieval step itself and asks the model to answer from those results.
- This is a separate chat path. /v1/assess stays deterministic (System 2 does not call tools on /explain).
"""
from __future__ import annotations

import asyncio
import json
import logging
import operator
import re
import time
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from .assess import Deadline
from .clients import MCPClient, ServiceUnavailable, allowed_tool, openai_tool, short_error
from .config import Settings

log = logging.getLogger("orchestrator.ask")

TOOL_RESULT_CHARS = 4000       # per tool result sent back to the model
CONTEXT_CHARS = 9000           # retrieved context for the fallback answer
TOOL_CALL_TIMEOUT_S = 10.0
TOOLS_TTL_S = 300.0
MAX_CITATIONS = 12
THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
BRACKETS = re.compile(r"\[([^\[\]]{1,300})\]")
ID_CHAR = r"[\w:/.\-]"     # characters an evidence id can contain (manual:12, reading:R0042, s1:live-action-x)

SYSTEM = """You are SPOTbot, the SPOT operator assistant for a plant where a Boston Dynamics Spot robot runs inspection missions.
Answer the operator's question using ONLY the results of the read-only tools you can call (flags, actions, missions,
the Spot manual, the triage policy, past incidents, operator notes, reliability statistics).
Rules: cite the `id` of every item you use in square brackets, e.g. [manual:12] or [F2]. Never invent ids, numbers or
procedures. If the tools return nothing relevant, say so plainly. Use at most a few tool calls, then answer in under
150 words."""

CONTEXT_SYSTEM = """You are SPOTbot, the SPOT operator assistant. Answer the operator's question using ONLY the retrieved context
below. Cite the `id` of every item you use in square brackets, e.g. [manual:12]. Never invent ids, numbers or
procedures. If the context does not answer the question, say so. Answer in under 150 words."""


class AskState(TypedDict, total=False):
    question: str
    flag_id: Optional[int]
    request_id: Optional[str]
    max_calls: int
    deadline: Deadline
    messages: Annotated[List[BaseMessage], add_messages]
    calls: int
    turns: int
    tool_log: Annotated[List[Dict[str, Any]], operator.add]
    retrieved: Annotated[List[Dict[str, Any]], operator.add]
    nxt: str
    mode: str
    model: Optional[str]
    answer: Optional[str]
    error: Optional[str]


def collect_ids(data: Any, out: Optional[List[str]] = None) -> List[str]:
    """Doc/evidence ids in a tool result: every `id` string and every `citation_ids` entry, in order."""
    out = [] if out is None else out

    def add(x: Any) -> None:
        if isinstance(x, str) and x and x not in out:
            out.append(x)

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            add(x.get("id"))
            for c in x.get("citation_ids") or []:
                add(c)
            for k, v in x.items():
                if k not in ("id", "citation_ids"):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    return out


def cited_ids(answer: str, ids: List[str]) -> List[str]:
    """Retrieved ids the answer actually cites: a whole `[id]` (comma/semicolon lists allowed inside the brackets) or
    a whole bare token, never a substring (`manual:1` does not match `[manual:12]`, `F2` does not match `F21`)."""
    bracketed = {part.strip() for m in BRACKETS.finditer(answer or "") for part in re.split(r"[,;]", m.group(1))}
    out = []
    for i in ids:
        if i in bracketed or re.search(r"(?<!%s)%s(?!%s)" % (ID_CHAR, re.escape(i), r"[\w:/\-]|\.\w"), answer or ""):
            out.append(i)
    return out


def _text(content: Any) -> str:
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return THINK.sub("", str(content or "")).strip()


def _dump(data: Any, limit: int) -> str:
    s = json.dumps({k: v for k, v in data.items() if not k.startswith("_")} if isinstance(data, dict) else data,
                   separators=(",", ":"), ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "...(truncated)"


def sources_answer(retrieved: List[Dict[str, Any]], why: str) -> str:
    """No-model answer: the most relevant retrieved items, verbatim titles/snippets, no generated text."""
    lines = []
    for r in retrieved:
        d = r.get("data") or {}
        for item in d.get("results") or []:
            if isinstance(item, dict) and item.get("id"):
                lines.append("- [%s] %s: %s" % (item["id"], item.get("title") or item.get("kind") or "",
                                                str(item.get("text") or "")[:200]))
        if r.get("tool") == "get_flag_context" and isinstance(d.get("flag"), dict):
            f = d["flag"]
            lines.append("- [%s] %s (%s)" % (f.get("id"), f.get("title"), f.get("severity")))
    if not lines:
        return "No answer: %s, and no sources were retrieved." % why
    return "The model is unavailable (%s). Most relevant sources:\n%s" % (why, "\n".join(lines[:6]))


class AskRunner:
    """Holds the chat model, the MCP client and the cached tool list; builds and runs the ask graph."""

    def __init__(self, settings: Settings, mcp: MCPClient, chat):
        self.s = settings
        self.mcp = mcp
        self.chat = chat            # a LangChain chat model (ChatOpenAI at the gateway); None = no model
        self._tools: Optional[List[Dict[str, Any]]] = None
        self._tools_at = 0.0
        self.graph = self._build()

    async def tools(self, request_id: Optional[str]) -> List[Dict[str, Any]]:
        """Allowlisted MCP tools (cached TOOLS_TTL_S); [] when the MCP server is unreachable."""
        if self._tools is not None and time.monotonic() - self._tools_at < TOOLS_TTL_S:
            return self._tools
        try:
            async with self.mcp.session(request_id) as sess:
                listed = await asyncio.wait_for(sess.list_tools(), 5.0)
        except (ServiceUnavailable, asyncio.TimeoutError) as exc:
            log.warning("MCP tools/list failed: %s", short_error(exc))
            return []
        self._tools = [t for t in listed if t.get("name") and allowed_tool(t, self.s.ask_tool_allowlist)]
        self._tools_at = time.monotonic()
        return self._tools

    def _model_name(self, ai: Any) -> str:
        meta = getattr(ai, "response_metadata", None) or {}
        return meta.get("model_name") or meta.get("model") or self.s.ask_model

    async def _call_tool(self, name: str, args: Dict[str, Any], state: AskState, by: str):
        t0 = time.perf_counter()
        budget = min(TOOL_CALL_TIMEOUT_S, state["deadline"].remaining(1.0))
        entry = {"name": name, "args": args, "by": by}
        if budget <= 0.2:
            return None, "error: no time left", {**entry, "ms": 0, "status": "skipped"}
        try:
            data = await asyncio.wait_for(self.mcp.call_tool(name, args, request_id=state.get("request_id")), budget)
        except ServiceUnavailable as exc:
            return None, "error: %s" % exc.reason, {**entry, "ms": int((time.perf_counter() - t0) * 1000),
                                                     "status": "error"}
        except asyncio.TimeoutError:
            return None, "error: timed out", {**entry, "ms": int((time.perf_counter() - t0) * 1000),
                                              "status": "timeout"}
        return data, _dump(data, TOOL_RESULT_CHARS), {**entry, "ms": int((time.perf_counter() - t0) * 1000),
                                                      "status": "ok"}

    def _build(self):
        async def agent(state: AskState) -> Dict[str, Any]:
            dl: Deadline = state["deadline"]
            turns = state.get("turns", 0) + 1
            calls = state.get("calls", 0)
            remaining = dl.remaining(1.0)
            if self.chat is None:
                return {"nxt": "fallback_retrieve" if calls == 0 else "finish", "turns": turns,
                        "error": "no chat model configured (GATEWAY_URL unset)"}
            if remaining < 1.0:
                return {"nxt": "finish", "turns": turns, "error": "deadline reached"}
            tools = await self.tools(state.get("request_id")) if calls < state["max_calls"] else []
            if not tools and calls == 0:
                return {"nxt": "fallback_retrieve", "turns": turns, "error": "no MCP tools available"}
            msgs = list(state["messages"])
            if not tools:
                msgs.append(HumanMessage("Tool budget used (%d calls). Answer now from the tool results above, "
                                         "citing their ids." % calls))
            runnable = self.chat.bind_tools([openai_tool(t) for t in tools]) if tools else self.chat
            try:
                ai = await asyncio.wait_for(runnable.ainvoke(msgs), dl.remaining(1.0))
            except asyncio.TimeoutError:
                return {"nxt": "finish", "turns": turns, "error": "deadline: the model did not answer in time"}
            except Exception as exc:  # noqa: BLE001 - openai/httpx errors, e.g. tools unsupported without --jinja
                why = short_error(exc)
                log.info("ask model call failed: %s", why)
                return {"nxt": "fallback_retrieve" if calls == 0 else "finish", "turns": turns,
                        "error": "model call failed: %s" % why}
            tool_calls = list(getattr(ai, "tool_calls", None) or []) if tools else []
            if tool_calls:
                return {"messages": [ai], "nxt": "tools", "turns": turns, "mode": "tools",
                        "model": self._model_name(ai)}
            if calls == 0:   # answered without looking anything up: ground it with one retrieval step
                return {"nxt": "fallback_retrieve", "turns": turns, "model": self._model_name(ai),
                        "error": "model made no tool call"}
            return {"messages": [ai], "nxt": "finish", "turns": turns, "answer": _text(ai.content) or None,
                    "model": self._model_name(ai)}

        async def tools_node(state: AskState) -> Dict[str, Any]:
            last = state["messages"][-1]
            allowed = {t["name"] for t in await self.tools(state.get("request_id"))}
            calls, cap = state.get("calls", 0), state["max_calls"]
            msgs: List[ToolMessage] = []
            logs: List[Dict[str, Any]] = []
            got: List[Dict[str, Any]] = []
            for tc in getattr(last, "tool_calls", None) or []:
                name, args, tid = tc.get("name"), tc.get("args") or {}, tc.get("id") or "call"
                if calls >= cap:
                    msgs.append(ToolMessage("refused: tool budget exhausted (%d calls); answer now" % cap,
                                            tool_call_id=tid))
                    logs.append({"name": name, "args": args, "by": "model", "ms": 0, "status": "skipped_cap"})
                    continue
                calls += 1
                if name not in allowed:
                    msgs.append(ToolMessage("refused: %s is not an allowed read-only tool" % name, tool_call_id=tid))
                    logs.append({"name": name, "args": args, "by": "model", "ms": 0, "status": "refused"})
                    continue
                data, content, entry = await self._call_tool(name, args, state, "model")
                msgs.append(ToolMessage(content, tool_call_id=tid))
                logs.append(entry)
                if data is not None:
                    got.append({"tool": name, "data": data})
            return {"messages": msgs, "calls": calls, "tool_log": logs, "retrieved": got}

        async def fallback_retrieve(state: AskState) -> Dict[str, Any]:
            q = state["question"][:500]
            plan = [("search_spot_manual", {"query": q, "k": 3}), ("search_policy", {"query": q, "k": 2})]
            if state.get("flag_id"):
                plan.append(("get_flag_context", {"flag_id": state["flag_id"]}))
            else:
                plan.append(("search_similar_incidents", {"text": q, "k": 3}))
            res = await asyncio.gather(*[self._call_tool(n, a, state, "fallback") for n, a in plan])
            return {"mode": "retrieval_fallback", "tool_log": [r[2] for r in res],
                    "retrieved": [{"tool": n, "data": r[0]} for (n, _), r in zip(plan, res) if r[0] is not None]}

        async def answer_from_context(state: AskState) -> Dict[str, Any]:
            retrieved = state.get("retrieved") or []
            if not retrieved:
                return {"mode": "no_context", "answer": None,
                        "error": (state.get("error") or "") + "; retrieval returned nothing"}
            if self.chat is None or state["deadline"].remaining(1.0) < 1.0:
                return {"mode": "no_model"}
            ctx = "\n".join("%s -> %s" % (r["tool"], _dump(r["data"], 4000)) for r in retrieved)[:CONTEXT_CHARS]
            msgs = [SystemMessage(CONTEXT_SYSTEM),
                    HumanMessage("Question: %s\n\nRetrieved context:\n%s" % (state["question"], ctx))]
            try:
                ai = await asyncio.wait_for(self.chat.ainvoke(msgs), state["deadline"].remaining(1.0))
            except asyncio.TimeoutError:
                return {"mode": "no_model", "error": "deadline: the model did not answer in time"}
            except Exception as exc:  # noqa: BLE001
                return {"mode": "no_model", "error": "model call failed: %s" % short_error(exc)}
            return {"answer": _text(ai.content) or None, "model": self._model_name(ai), "turns": state.get("turns", 0) + 1}

        async def finish(state: AskState) -> Dict[str, Any]:
            if state.get("answer"):
                return {}
            mode = state.get("mode")
            mode = mode if mode == "no_context" else "no_model"
            return {"answer": sources_answer(state.get("retrieved") or [], state.get("error") or "no answer"),
                    "mode": mode, "model": None}

        g = StateGraph(AskState)
        g.add_node("agent", agent)
        g.add_node("tools", tools_node)
        g.add_node("fallback_retrieve", fallback_retrieve)
        g.add_node("answer_from_context", answer_from_context)
        g.add_node("finish", finish)
        g.add_edge(START, "agent")
        g.add_conditional_edges("agent", lambda st: st.get("nxt", "finish"),
                                {"tools": "tools", "fallback_retrieve": "fallback_retrieve", "finish": "finish"})
        g.add_edge("tools", "agent")
        g.add_edge("fallback_retrieve", "answer_from_context")
        g.add_edge("answer_from_context", "finish")
        g.add_edge("finish", END)
        return g.compile()

    async def run(self, question: str, flag_id: Optional[int], max_steps: Optional[int],
                  request_id: Optional[str]) -> Dict[str, Any]:
        t0 = time.perf_counter()
        cap = max(1, min(self.s.ask_max_tool_calls, max_steps or self.s.ask_max_tool_calls))
        user = question if not flag_id else ("%s\n\n(The operator is looking at flag %d; get_flag_context(%d) "
                                             "returns its evidence packet.)" % (question, flag_id, flag_id))
        state: AskState = {"question": question, "flag_id": flag_id, "request_id": request_id, "max_calls": cap,
                           "deadline": Deadline(self.s.ask_deadline_s), "calls": 0, "turns": 0,
                           "messages": [SystemMessage(SYSTEM), HumanMessage(user)], "tool_log": [], "retrieved": [],
                           "mode": "tools"}
        try:
            out = await asyncio.wait_for(self.graph.ainvoke(state, {"recursion_limit": 40}),
                                         self.s.ask_deadline_s + 2.0)
        except asyncio.TimeoutError:
            out = {**state, "answer": "No answer: the request deadline (%gs) passed." % self.s.ask_deadline_s,
                   "mode": "no_model", "model": None}
        answer = out.get("answer") or ""
        retrieved_ids: List[str] = []
        for r in out.get("retrieved") or []:
            collect_ids(r.get("data"), retrieved_ids)
        cited = cited_ids(answer, retrieved_ids)
        return {"answer": answer, "citations": cited[:MAX_CITATIONS], "cited": bool(cited),
                "retrieved": retrieved_ids[:MAX_CITATIONS],
                "tool_calls": out.get("tool_log") or [], "model": out.get("model"), "mode": out.get("mode"),
                "steps": out.get("turns", 0), "max_tool_calls": cap, "error": out.get("error"),
                "latency_ms": int((time.perf_counter() - t0) * 1000), "request_id": request_id}
