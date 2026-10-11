"""Answer mode for one chat turn (docs/chat-contract.md §10, `chat.4`).

Only questions about the plant are grounded (retrieval + citations + the grounding check). Greetings and general
questions get a general answer: no retrieval, no `<sources>` block, no citation rule. Plain rules, no model call, so
the one-Gemini-call-per-turn rule (§0) holds. The first rule that matches wins; anything unsure is grounded, or probed
(retrieval decides) — never answered from general knowledge on a guess.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, NamedTuple, Optional

from .retrieval import Prior, extract_entities

GROUNDED, GENERAL, PROBE = "grounded", "general", "probe"


class Route(NamedTuple):
    mode: str      # grounded | general | probe
    reason: str    # the rule that decided: mission_chat | names_id | plant_terms | follow_up | small_talk | unsure


# Plant vocabulary: a question with any of these is about SPOTlight data (rule 3). Kept broad on purpose — a false
# "grounded" only costs a retrieval; a false "general" could invent plant facts.
PLANT_TERMS = re.compile(r"""\b(
    missions? | flag(?:s|ged)? | p[1-4] | priorit(?:y|ies) | escalat\w* | acknowledg\w* | triage\w* |
    anomal(?:y|ies|ous) | incidents? | alerts? | alarms? |
    assets? | equipment | inspect\w* | waypoints? | locations? |
    fail\w* | faults? | broke\w* | batter(?:y|ies) | charg\w* | dock\w* | fleet | robots? | spot | autowalk |
    gauges? | readings? | sensors? | thermal | acoustic | navigation | localization |
    temperatures? | pressures? | vibration | leak\w* | corrosion | overheat\w* |
    pumps? | valves? | panels? | compressors? | motors? | heat\s+exchangers? | turbines? | transformers? |
    status | statistics | success\s+rates? | reliab\w* | trends? | kpis? | health | risks? | summar\w* |
    v1 | v2 | configurations? | worst | latest | today | yesterday | how\s+many | how\s+often |
    our | we | operators? | shift | plant | facility | nextera
)\b""", re.I | re.X)

# Small talk and questions about SPOTbot itself (rule 5): the whole message, after trimming punctuation.
SMALL_TALK = re.compile(r"""^(
    (hi|hello|hey|hiya|howdy|yo|sup|greetings)(\s+(there|spotbot|spot\s*bot|team|all|everyone))? |
    good\s+(morning|afternoon|evening|night) |
    (thanks|thank\s+you|thx|ty|cheers)(\s+(so\s+much|a\s+lot|spotbot|again))? |
    (ok|okay|k|cool|great|nice|awesome|perfect|got\s+it|sounds\s+good|understood|sure|yes|no|yep|nope) |
    (bye|goodbye|see\s+you|later|cya) |
    (who|what)\s+are\s+you | what\s+is\s+(spotbot|spot\s*bot|your\s+name) | who\s+made\s+you |
    what\s+(can|do)\s+you\s+do | how\s+(can|do)\s+you\s+help(\s+me)? | what\s+can\s+i\s+ask(\s+you)? |
    how\s+do\s+(you|i\s+use\s+you)\s+work | help | how\s+are\s+you(\s+doing)? | are\s+you\s+there
)$""", re.I | re.X)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s'-]", " ", text or "").split()).strip().lower()


def is_small_talk(question: str) -> bool:
    return bool(SMALL_TALK.match(_norm(question)))


def names_plant_id(text: Optional[str]) -> bool:
    return any(extract_entities(text).values())


def choose(question: str, previous: Optional[str], prior: Optional[Prior], mission_id: Optional[str]) -> Route:
    """The answer mode for this turn (§10). `previous` = the previous user message, `prior` = the previous answer."""
    if mission_id:
        return Route(GROUNDED, "mission_chat")
    if names_plant_id(question):
        return Route(GROUNDED, "names_id")
    small = is_small_talk(question)
    if not small and PLANT_TERMS.search(question or ""):
        return Route(GROUNDED, "plant_terms")
    if not small and ((prior is not None and prior.cited) or names_plant_id(previous)):
        return Route(GROUNDED, "follow_up")
    if small:
        return Route(GENERAL, "small_talk")
    return Route(PROBE, "unsure")


# What counts as plant context for a probe: records of this plant's missions, actions, anomalies, flags, assets and
# waypoints. The Spot user guide, policy text and hub nodes (topic, failure_type, ...) match general robot questions
# too ("how does lidar work?" hits the manual's safety pages), so they don't turn a general question into a grounded
# one; a question that is about the plant already grounds by its words (rule 3).
PLANT_KINDS = {"mission", "action", "anomaly", "flag", "asset", "location",
               "event", "incident", "operator_note", "explanation"}


def has_evidence(sources: List[Dict[str, Any]], carried: Optional[List[str]] = None) -> bool:
    """A probe grounds when retrieval found plant context: a source of a PLANT_KINDS kind that is not the fleet
    snapshot (`stat:*`) or carry-over (a digest, an event/incident document above the §2 floor, an instance node)."""
    skip = set(carried or [])
    return any(s.get("kind") in PLANT_KINDS and not str(s.get("id", "")).startswith("stat:") and s.get("id") not in skip
               for s in sources)


def fleet_ids(answer: str) -> List[str]:
    """Plant ids a general answer names anyway (§10 `fleet_ids`), in a stable order."""
    ents = extract_entities(answer)
    return ents["missions"] + ents["assets"] + ents["locations"] + ["flag %s" % f for f in ents["flags"]]
