"""Model catalog and per-family thinking config for the AI Assistance chat (docs/chat-contract.md §0, §4 /models).

The thinking table was measured against the team key on 2026-10-10:
- `gemini-3*`: fast = thinkingLevel "low" (not "minimal": gemini-3.8-flash rejects it); thinking = "high" with
  includeThoughts, so the stream carries thought summaries.
- `gemma-*`: NO thinkingConfig at all (a 400 if sent). The model may still emit `thought: true` parts: they are
  dropped in fast mode and streamed as thinking in thinking mode. Gemma is opt-in (`chat.2`): not in the default
  CHAT_MODELS (37 s to the first byte, then a 503, on 2026-10-10), but still known here.
- titles: `minimal` for gemini, nothing for gemma (`title_thinking_config`).
CHAT_MODELS picks and orders the picker; an id missing from KNOWN still works (label derived from the id), so a
model can be added from the environment without a code change.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

MODES = ("fast", "thinking")

MODE_INFO = [
    {"id": "fast", "label": "Fast", "description": "Answers right away with light reasoning. Best for lookups."},
    {"id": "thinking", "label": "Thinking",
     "description": "Reasons step by step before answering and shows a summary of its thinking. Slower."},
]

# id -> label, description, thinking_summaries (whether thinking mode returns readable thought text)
KNOWN: Dict[str, Dict[str, Any]] = {
    "gemini-3.8-flash": {"label": "Gemini 3.8 Flash", "thinking_summaries": True,
                         "description": "Most capable. Best for comparisons and multi-step questions."},
    "gemini-3.5-flash": {"label": "Gemini 3.5 Flash", "thinking_summaries": True,
                         "description": "Fast and capable. A good default when 3.8 is rate limited."},
    "gemini-3.5-flash-lite": {"label": "Gemini 3.5 Flash-Lite", "thinking_summaries": False,
                              "description": "Fastest Gemini. Thinks, but may not show its thinking."},
    "gemini-3.1-flash-lite": {"label": "Gemini 3.1 Flash-Lite", "thinking_summaries": False,
                              "description": "Small and quick. Used for chat titles."},
    "gemma-4-31b-it": {"label": "Gemma 4 31B", "thinking_summaries": True,
                       "description": "Open model with its own quota. Slower, useful when Gemini is rate limited."},
    "gemma-4-26b-a4b-it": {"label": "Gemma 4 26B A4B", "thinking_summaries": True,
                           "description": "Open mixture-of-experts model with its own quota."},
}


def family(model: str) -> str:
    return "gemma" if model.lower().startswith("gemma") else "gemini"


def _label(model: str) -> str:
    return " ".join(w.upper() if w[:1].isdigit() and w.endswith("b") else w.capitalize()
                    for w in model.replace("-it", "").split("-"))


def model_info(model: str) -> Dict[str, Any]:
    k = KNOWN.get(model, {})
    return {"id": model, "label": k.get("label") or _label(model), "description": k.get("description", ""),
            "family": family(model), "thinking_summaries": bool(k.get("thinking_summaries", family(model) == "gemma"))}


def catalog(models: List[str]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for m in models:
        if m and m not in seen:
            seen.add(m)
            out.append(model_info(m))
    return out


def thinking_config(model: str, mode: str) -> Optional[Dict[str, Any]]:
    """`generationConfig.thinkingConfig` for this model and mode, or None (gemma: never sent)."""
    if family(model) == "gemma":
        return None
    if mode == "thinking":
        return {"thinkingLevel": "high", "includeThoughts": True}
    return {"thinkingLevel": "low"}


def title_thinking_config(model: str) -> Optional[Dict[str, Any]]:
    """The title call (`chat.2`): `minimal` thinking (0.7 s on gemini-3.1-flash-lite vs 18.9 s at `low`), none for
    gemma. A model that rejects `minimal` (gemini-3.8-flash) just keeps the auto title."""
    return None if family(model) == "gemma" else {"thinkingLevel": "minimal"}


def show_thoughts(mode: str) -> bool:
    """Thought parts are streamed only in thinking mode (any family); in fast mode they are dropped."""
    return mode == "thinking"
