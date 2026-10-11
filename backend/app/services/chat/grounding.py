"""Deterministic grounding check on a finished (or stopped) answer (docs/chat-contract.md §2 "Grounding check").

The citation rule is shared with the web (`chat.2`); both test suites run the same fixtures:
- A citation group is `[` ... `]` on one line, not inside inline or fenced code, and not a Markdown link's text
  (`[text](url)`). Its inside is split on `,` and `;`; each part is trimmed of whitespace, quotes and backticks.
- A part is *id-shaped* if it matches `^[A-Za-z_]+:[\\w.:/\\-]+$`. A part *cites* a source when it equals a source
  id case-insensitively (it then counts as the canonical source id).
- A group renders as chips when at least one part cites; the citing parts are the chips. Otherwise it is literal text.
- `cited_ids`: canonical ids cited, in order of first use. `unknown_ids`: id-shaped parts that cite nothing.
`unverified_numbers`: numbers in the answer (outside brackets, not part of an id such as M007, HX-204, WP09, A0133)
that appear in neither the source texts sent to the model nor the operator's question. Compared normalized: `62.50`
= `62.5`, `%` stripped, thousands separators removed, leading zeros dropped. Dates and clock times are read part by
part (`-`, `:`, `/` and `T` between digits all split), and ordered-list markers / numbered headings are not numbers.
`grounded = len(cited_ids) >= 1 and not unknown_ids`. No model is involved: this runs after the stream, in
microseconds, and is stored on the message.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ID_SHAPE = re.compile(r"^[A-Za-z_]+:[\w.:/\-]+$")
# A group: no nested brackets, one line, not followed by `(` (that is a Markdown link).
GROUP = re.compile(r"\[([^\[\]\n]+)\](?!\()")
PART_SPLIT = re.compile(r"[,;]")
PART_TRIM = " \t\r\n'\"`“”‘’"
_FENCE = re.compile(r"(?ms)^[ ]{0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^[ ]{0,3}\1[`~]*[ \t]*$|\Z)")
_INLINE_CODE = re.compile(r"(?s)(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_BRACKET = re.compile(r"\[[^\[\]\n]*\]")
# A token that starts with letters and runs into digits is an identifier, not a figure: M007, HX-204, WP09, A0133,
# p52-1, V2, gfx1100. Removed before numbers are read, on both sides of the comparison.
_ID_TOKEN = re.compile(r"\b[A-Za-z][A-Za-z_]*-?\d[\w-]*")
# `1.` / `2)` at line start (also after a blockquote `>` or a heading's `#`s) is a list marker or a section number.
_LIST_MARKER = re.compile(r"(?m)^([ \t>]*(?:#{1,6}[ \t]+)?)\d+[.)](?=\s)")
# `2026-09-01T18:00:00`: the T between digits splits like `-` and `:` do.
_DATE_T = re.compile(r"(?<=\d)T(?=\d)")
_NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")


def normalize_number(tok: str) -> str:
    t = tok.rstrip("%").replace(",", "")
    whole, _, frac = t.partition(".")
    whole = whole.lstrip("0") or "0"
    frac = frac.rstrip("0")
    return whole + ("." + frac if frac else "")


def _figures(text: str) -> List[str]:
    """Numbers as written, after ids are removed and date/time `T`s are split."""
    return _NUMBER.findall(_ID_TOKEN.sub(" ", _DATE_T.sub(" ", text or "")))


def numbers_in(text: str) -> List[str]:
    """The figures in an answer as written (ids, bracket groups and list markers excluded)."""
    t = _BRACKET.sub(" ", text or "")
    t = _LIST_MARKER.sub(r"\1", t)
    return _figures(t)


def number_set(texts: Iterable[str]) -> set:
    out = set()
    for t in texts:
        out.update(normalize_number(n) for n in _figures(t or ""))
    return out


def _code_spans(text: str) -> List[Tuple[int, int]]:
    """(start, end) of fenced code blocks and inline code spans."""
    spans = [m.span() for m in _FENCE.finditer(text)]

    def inside(i: int) -> bool:
        return any(a <= i < b for a, b in spans)
    spans += [m.span() for m in _INLINE_CODE.finditer(text) if not inside(m.start())]
    return spans


def citation_groups(text: str, source_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """Every bracket group outside code, in order: {start, end, raw, chips (canonical ids), unknown (id-shaped
    parts that cite nothing)}. A group with no chips renders as its literal text."""
    text = text or ""
    folded = {}
    for s in source_ids:
        folded.setdefault(s.lower(), s)
    code = _code_spans(text)
    out: List[Dict[str, Any]] = []
    for m in GROUP.finditer(text):
        if any(a <= m.start() < b for a, b in code):
            continue
        chips: List[str] = []
        unknown: List[str] = []
        for raw in PART_SPLIT.split(m.group(1)):
            part = raw.strip(PART_TRIM)
            if not part:
                continue
            sid = folded.get(part.lower())
            if sid is not None:
                chips.append(sid)
            elif ID_SHAPE.match(part):
                unknown.append(part)
        out.append({"start": m.start(), "end": m.end(), "raw": m.group(0), "chips": chips, "unknown": unknown})
    return out


def citations_in(text: str, source_ids: Sequence[str]) -> Tuple[List[str], List[str]]:
    """(cited_ids, unknown_ids), each de-duplicated in order of first use."""
    cited: List[str] = []
    unknown: List[str] = []
    for g in citation_groups(text, source_ids):
        cited += [c for c in g["chips"] if c not in cited]
        for u in g["unknown"]:
            if u not in unknown:
                unknown.append(u)
    return cited, unknown


def strip_citations(text: str) -> str:
    """The text without its citation markers: every group with an id-shaped part goes (with the space before it).
    Used on earlier answers before they go back to the model as history, so it can't copy an old id."""
    def repl(m: "re.Match[str]") -> str:
        parts = [p.strip(PART_TRIM) for p in PART_SPLIT.split(m.group(1))]
        return "" if any(ID_SHAPE.match(p) for p in parts if p) else m.group(0)
    return re.sub(r"[ \t]*\[([^\[\]\n]+)\](?!\()", repl, text or "")


def check(answer: str, sources: Sequence[Dict[str, Any]], source_texts: Iterable[str], question: str,
          retrieval_mode: Optional[str] = None, graph_seeds: Optional[List[str]] = None) -> Dict[str, Any]:
    ids = [s["id"] for s in sources]
    cited, unknown = citations_in(answer, ids)
    known_numbers = number_set(list(source_texts) + [question or ""])
    unverified: List[str] = []
    seen = set()
    for tok in numbers_in(answer):
        n = normalize_number(tok)
        if n not in known_numbers and n not in seen:
            seen.add(n)
            unverified.append(tok)
    return {"grounded": len(cited) >= 1 and not unknown, "cited_ids": cited, "unknown_ids": unknown,
            "unverified_numbers": unverified, "source_count": len(ids), "retrieval_mode": retrieval_mode,
            "graph_seeds": list(graph_seeds or []), "mode": "grounded"}


def general_check(fleet_ids: List[str]) -> Dict[str, Any]:
    """chat.4 (§10): a general answer has nothing to check against; it records the plant ids it names anyway."""
    return {"grounded": False, "cited_ids": [], "unknown_ids": [], "unverified_numbers": [], "source_count": 0,
            "retrieval_mode": None, "graph_seeds": [], "mode": "general", "fleet_ids": list(fleet_ids)}
