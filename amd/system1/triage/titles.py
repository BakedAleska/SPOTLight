"""The one-line title of a flag: what happened, on which asset, at which waypoint.

One function for every flag, so seeded flags (db/setup.py) and flags raised by replay, live runs and ingest
(backend validator) read the same. The queue row already shows the severity badge, the confidence, the asset and
location names, the mission and every reason, so the title repeats none of them; the battery appears only when it is
the problem. Pure Python, no torch: the backend ships this module to Vercel (backend/vercel_build.py).
"""
from typing import Optional

from .policy import reading_state

# policy.PLAUSIBLE's measurement types, without their units
MEASURES = {"TEMPERATURE_C": "temperature", "PRESSURE_PSI": "pressure", "SOUND_DB": "sound level",
            "VISUAL_SCORE": "visual score", "LOCALIZATION_CONFIDENCE_PCT": "localization confidence"}


def _h(s: Optional[str]) -> str:
    """ACOUSTIC_INSPECTION -> acoustic inspection."""
    return (s or "").replace("_", " ").lower()


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _mission_title(ev: dict) -> str:
    s = ev["anomalies_by_severity"]
    serious = s["HIGH"] + s["CRITICAL"]
    bits = []
    if ev["actions_failed"]:
        bits.append("%d of %d actions failed" % (ev["actions_failed"], ev["actions_total"]))
    if serious:
        bits.append("%d serious anomal%s" % (serious, "y" if serious == 1 else "ies"))
    mission = "Mission %s" % ev["mission_id"]
    if ev["reported_status"] == "COMPLETED":
        return "%s marked complete, but %s" % (mission, " and ".join(bits)) if bits else "%s completed" % mission
    # "Mission M017 failed (3 of 10 actions, 1 serious anomaly)": the status already says failed
    if ev["actions_failed"]:
        bits[0] = "%d of %d actions" % (ev["actions_failed"], ev["actions_total"])
    return "%s %s%s" % (mission, _h(ev["reported_status"]), (" (" + ", ".join(bits) + ")") if bits else "")


def flag_title(ev: dict, category: Optional[str] = None) -> str:
    """`ev` is a triage.events action or mission event; `category` the flag's category (robot_health, ...)."""
    if ev["kind"] == "mission":
        return _mission_title(ev)
    where = "%s (%s)" % (ev["asset"]["asset_id"], ev["location"]["location_id"])
    an = ev.get("anomaly")
    if an:
        return "%s on %s" % (_cap(_h(an["anomaly_type"])), where)
    if ev.get("status") == "FAILED":
        cause = (": " + _h(ev.get("failure_type"))) if ev.get("failure_type") else ""
        return "%s failed%s on %s" % (_cap(_h(ev["action_type"])), cause, where)
    if category == "robot_health":
        return "Low battery (%d%%) at %s" % (ev["battery_pct"], where)
    trust, out_of_band = reading_state(ev)
    if trust != "none":
        kind = ev["reading"]["measurement_type"]
        measure = MEASURES.get(kind, _h(kind))
        if out_of_band:
            return "%s out of safe band on %s" % (_cap(measure), where)
        if trust == "untrusted":
            return "Untrusted %s reading on %s" % (measure, where)
    return "%s needs review on %s" % (_cap(_h(ev["action_type"])), where)
