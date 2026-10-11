"""Reference data (the challenge kit) and the replay-event -> canonical-event builder.

The kit CSVs are the same rows `db/` seeds into Neon, loaded once into memory with
`amd/system1/triage/kit.py`. Ingest uses them to:
- enrich a replay event with its action's reading and anomaly (the stream lacks them; a documented
  assumption in research/spot-data-insights-2026-10-09.md), and
- build the canonical event (`triage/events.py`) that System 1 and the validator read.

Events for actions the kit doesn't know still work if the robot sent enough detail: ACTION_STARTED details
are cached so the matching ACTION_COMPLETED/FAILED can be completed from them.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .triage import T_events, T_kit

log = logging.getLogger("backend.reference")

ACTION_RESULT_TYPES = ("ACTION_COMPLETED", "ACTION_FAILED")
EVENT_TYPES = ("MISSION_STARTED", "ACTION_STARTED", "ACTION_COMPLETED", "ACTION_FAILED", "MISSION_COMPLETED")


def parse_ts(v: Any) -> Optional[dt.datetime]:
    """ISO string -> naive datetime (the DB columns are `timestamp`, robot local time)."""
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=None)


def _int(v: Any) -> Optional[int]:
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


class Reference:
    def __init__(self, kit_dir: Path):
        self.kit_dir = Path(kit_dir)
        try:
            kit = T_kit.load_kit(self.kit_dir)
        except TypeError:  # older kit.py without the kit_dir argument
            kit = T_kit.load_kit()
        self.kit = kit
        self.locations: Dict[str, dict] = kit.locations
        self.assets: Dict[str, dict] = kit.assets
        self.missions: Dict[str, dict] = kit.missions
        self.actions: Dict[str, dict] = {a["action_id"]: a for a in kit.actions}
        self.readings: Dict[str, dict] = kit.readings
        self.anomalies: Dict[str, dict] = kit.anomalies
        # kit.py drops the row ids; keep them so payloads can cite reading:<id> / anomaly:<id>.
        self.reading_ids = self._ids("readings.csv", "reading_id")
        self.anomaly_ids = self._ids("anomalies.csv", "anomaly_id")
        self._started: Dict[str, dict] = {}            # action_id -> ACTION_STARTED details (live cache)
        self._mission_started: Dict[str, dict] = {}    # mission_id -> MISSION_STARTED details

    def _ids(self, name: str, col: str) -> Dict[str, str]:
        try:
            with open(self.kit_dir / name, newline="", encoding="utf-8") as f:
                return {r["action_id"]: r[col] for r in csv.DictReader(f)}
        except (OSError, KeyError):
            return {}

    def reading_of(self, action_id: str) -> Optional[dict]:
        r = self.readings.get(action_id)
        return {"reading_id": self.reading_ids.get(action_id), **r} if r else None

    def anomaly_of(self, action_id: str) -> Optional[dict]:
        a = self.anomalies.get(action_id)
        return {"anomaly_id": self.anomaly_ids.get(action_id), **a} if a else None

    @property
    def replay_path(self) -> Path:
        return self.kit_dir / "replay.ndjson"

    def reset(self) -> None:
        self._started.clear()
        self._mission_started.clear()

    # ── records ─────────────────────────────────────────────────────────────────────────────────
    def mission(self, mission_id: Optional[str], overrides: Optional[dict] = None) -> Optional[dict]:
        base = self.missions.get(mission_id or "")
        started = self._mission_started.get(mission_id or "", {})
        if base is None and not started:
            return None
        m = dict(base or {"mission_id": mission_id, "mission_name": None, "scenario": started.get("scenario"),
                          "configuration_version": started.get("configuration_version"),
                          "start_time": None, "end_time": None, "duration_min": None,
                          "start_battery_pct": _int(started.get("battery_pct")), "end_battery_pct": None,
                          "mission_status": None, "operator_mode": None})
        if overrides:
            m.update({k: v for k, v in overrides.items() if v is not None})
        return m

    def action(self, action_id: Optional[str], details: Optional[dict] = None,
               timestamp: Optional[str] = None) -> Optional[dict]:
        """The action record: kit row, overlaid with cached ACTION_STARTED details, overlaid with `details`."""
        if not action_id:
            return None
        base = self.actions.get(action_id)
        src = (details or {}).get("source_action_id")
        if base is None and src in self.actions:
            # A live event (fresh action id) re-sending a kit row: the kit row is the base, the details win.
            base = dict(self.actions[src], action_id=action_id, timestamp=None)
        merged: Dict[str, Any] = dict(base) if base else {"action_id": action_id}
        for src in (self._started.get(action_id) or {}, details or {}):
            for k in ("mission_id", "location_id", "asset_id", "action_type", "status", "sequence",
                      "duration_sec", "configuration_version"):
                if src.get(k) not in (None, ""):
                    merged[k] = src[k]
            if src.get("battery_pct") not in (None, ""):
                merged["battery_pct"] = _int(src["battery_pct"])
            if "failure_type" in src:
                merged["failure_type"] = src["failure_type"] or None
        merged.setdefault("failure_type", None)
        merged.setdefault("duration_sec", None)
        if not merged.get("timestamp"):
            merged["timestamp"] = timestamp
        if merged.get("sequence") is None:
            merged["sequence"] = 1 + sum(1 for a in self._started.values()
                                         if a.get("mission_id") == merged.get("mission_id") and
                                         a.get("action_id") != action_id)
        return merged

    def mission_actions(self, mission_id: str) -> List[dict]:
        return [a for a in self.kit.actions if a["mission_id"] == mission_id]

    # ── normalize (pipeline step 2) ─────────────────────────────────────────────────────────────
    def normalize(self, raw: dict) -> Dict[str, Any]:
        """Columns for the `events` table plus the enriched payload. Also updates the live caches."""
        details = raw.get("details") or {}
        etype = raw.get("event_type")
        mission_id = raw.get("mission_id") or details.get("mission_id")
        action_id = details.get("action_id") or raw.get("action_id")
        if etype == "ACTION_STARTED" and action_id:
            self._started[action_id] = {**details, "mission_id": mission_id, "action_id": action_id}
        if etype == "MISSION_STARTED" and mission_id:
            self._mission_started[mission_id] = dict(details)

        location_id = asset_id = None
        battery = _int(details.get("battery_pct"))
        # Same shape as `db/setup.py seed`: the event's details, plus the joined reading/anomaly on results.
        payload: Dict[str, Any] = dict(details)
        if mission_id and mission_id not in self.missions and isinstance(details.get("live"), dict):
            # Live runs are not kit missions: events.mission_id references `missions`, so the live mission id
            # stays in the payload (details.mission / details.live) and in raw.spot_event_log only.
            mission_id = None
        if action_id:
            a = self.action(action_id, details) or {}
            location_id, asset_id = a.get("location_id"), a.get("asset_id")
            if battery is None:
                battery = a.get("battery_pct")
            if etype in ACTION_RESULT_TYPES and ("reading" in details or "anomaly" in details):
                rd, an = details.get("reading"), details.get("anomaly")   # live: sent with the event
            elif etype in ACTION_RESULT_TYPES:
                rd, an = self.reading_of(action_id), self.anomaly_of(action_id)
                if rd:
                    payload["reading"] = rd
                if an:
                    payload["anomaly"] = an
            enriched: Dict[str, Any] = {"action": {k: a.get(k) for k in (
                "sequence", "action_type", "location_id", "asset_id", "battery_pct", "status", "failure_type",
                "duration_sec")}}
            loc, asset = self.locations.get(location_id or ""), self.assets.get(asset_id or "")
            if loc:
                enriched["location"] = {k: loc.get(k) for k in ("location_name", "zone_type", "environment")}
            if asset:
                enriched["asset"] = {k: asset.get(k) for k in ("asset_name", "asset_type", "criticality")}
            payload["enriched"] = enriched
        elif etype == "MISSION_COMPLETED":
            battery = _int(details.get("end_battery_pct"))
        return {"event_id": raw.get("event_id"), "mission_id": mission_id, "ts": parse_ts(raw.get("timestamp")),
                "event_type": etype, "action_id": action_id, "location_id": location_id, "asset_id": asset_id,
                "battery_pct": battery, "payload": payload}

    # ── canonical event (pipeline step 3) ───────────────────────────────────────────────────────
    def canonical(self, raw: dict, source: str = "replay") -> Tuple[Optional[str], Optional[dict], Optional[str]]:
        """(kind, canonical event, why_not). kind is pre_action | action | mission, or None (with a reason)."""
        etype = raw.get("event_type")
        details = raw.get("details") or {}
        mission_id = raw.get("mission_id") or details.get("mission_id")
        if etype == "MISSION_STARTED":
            return None, None, "MISSION_STARTED carries nothing to triage"
        live = details.get("live") if isinstance(details.get("live"), dict) else None
        if etype == "MISSION_COMPLETED" and live and mission_id not in self.missions:
            return None, None, "live run summary (MISSION_COMPLETED of a live run) is stored, not triaged"
        if etype == "MISSION_COMPLETED":
            m = self.mission(mission_id, {"mission_status": details.get("status"),
                                          "end_battery_pct": _int(details.get("end_battery_pct")),
                                          "end_time": raw.get("timestamp")})
            if m is None:
                return None, None, "unknown mission %r" % mission_id
            missing = [k for k in ("configuration_version", "duration_min", "start_battery_pct", "end_battery_pct",
                                   "mission_status") if m.get(k) is None]
            if missing:
                return None, None, "mission %s lacks %s" % (mission_id, ", ".join(missing))
            history = [self._action_event(a, m, source) for a in self.mission_actions(mission_id)]
            return "mission", T_events.mission_event(m, [h for h in history if h], source), None
        if etype in ("ACTION_STARTED",) + ACTION_RESULT_TYPES:
            action_id = details.get("action_id") or raw.get("action_id")
            a = self.action(action_id, details, raw.get("timestamp"))
            if a is None:
                return None, None, "action event without action_id"
            if live and isinstance(details.get("mission"), dict) and (a.get("mission_id") or mission_id)                     not in self.missions:
                m = dict(details["mission"])     # live run: the mission record travels with every event
            else:
                m = self.mission(a.get("mission_id") or mission_id)
            loc, asset = self.locations.get(a.get("location_id") or ""), self.assets.get(a.get("asset_id") or "")
            if m is None or loc is None or asset is None:
                return None, None, "unknown %s" % ("mission" if m is None else "location" if loc is None else "asset")
            if a.get("battery_pct") is None or not a.get("action_type"):
                return None, None, "action %s lacks battery_pct/action_type" % action_id
            if m.get("configuration_version") is None:
                m["configuration_version"] = a.get("configuration_version")
            if etype == "ACTION_STARTED":
                if m.get("start_battery_pct") is None:
                    return None, None, "mission %s lacks start_battery_pct" % m["mission_id"]
                history = [self._action_event(h, m, source) for h in self.mission_actions(m["mission_id"])
                           if (h.get("sequence") or 0) < (a.get("sequence") or 0)]
                return "pre_action", T_events.pre_action_event(a, m, loc, asset, [h for h in history if h],
                                                               source), None
            a = dict(a, status=details.get("status") or ("FAILED" if etype == "ACTION_FAILED" else "SUCCESS"))
            if "failure_type" in details:
                a["failure_type"] = details.get("failure_type") or None
            if live and ("reading" in details or "anomaly" in details):
                rd = details.get("reading")
                rd = {k: v for k, v in rd.items() if k != "reading_id"} if isinstance(rd, dict) else None
                an = details.get("anomaly")
                an = {k: v for k, v in an.items() if k != "anomaly_id"} if isinstance(an, dict) else None
            else:
                rd, an = self.readings.get(action_id), self.anomalies.get(action_id)
            return "action", T_events.action_event(a, m, loc, asset, rd, an, source), None
        return None, None, "unknown event_type %r" % etype

    def _action_event(self, a: dict, m: dict, source: str) -> Optional[dict]:
        loc, asset = self.locations.get(a.get("location_id") or ""), self.assets.get(a.get("asset_id") or "")
        if loc is None or asset is None or a.get("status") not in ("SUCCESS", "FAILED"):
            return None
        return T_events.action_event(a, m, loc, asset, self.readings.get(a["action_id"]),
                                     self.anomalies.get(a["action_id"]), source)

    def canonical_for_action(self, action_id: str, kind: str = "pre_action", source: str = "forecast") -> Optional[dict]:
        """Canonical event for a kit action without a replay event (used by /system1/forecast)."""
        a = self.actions.get(action_id)
        if a is None:
            return None
        if kind == "pre_action":
            raw = {"event_type": "ACTION_STARTED", "mission_id": a["mission_id"], "timestamp": a["timestamp"],
                   "details": {"action_id": action_id}}
        else:
            raw = {"event_type": "ACTION_FAILED" if a["status"] == "FAILED" else "ACTION_COMPLETED",
                   "mission_id": a["mission_id"], "timestamp": a["timestamp"],
                   "details": {"action_id": action_id, "status": a["status"], "failure_type": a["failure_type"]}}
        _, ev, _ = self.canonical(raw, source)
        return ev
