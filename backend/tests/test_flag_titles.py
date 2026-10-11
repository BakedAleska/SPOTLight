"""Flag titles (amd/system1/triage/titles.py): one short line, the same for seeded and runtime flags."""
import copy

from app.services import validator as V
from app.services.triage import T_titles

ASSET = {"asset_id": "HX-208", "asset_name": "Heat Exchanger 08", "criticality": "HIGH"}
LOC = {"location_id": "WP03", "location_name": "Cooling Loop"}
ACTION = {"kind": "action", "event_id": "act-A0009", "mission_id": "M001", "action_type": "GAUGE_READING",
          "status": "SUCCESS", "failure_type": None, "battery_pct": 64, "asset": ASSET, "location": LOC,
          "reading": None, "anomaly": None}
MISSION = {"kind": "mission", "event_id": "mis-M017", "mission_id": "M017", "reported_status": "COMPLETED",
           "actions_total": 10, "actions_failed": 3, "anomalies_by_severity": {"LOW": 0, "MEDIUM": 1, "HIGH": 1, "CRITICAL": 0}}


def ev(base=ACTION, **kw):
    out = copy.deepcopy(base)
    out.update(kw)
    return out


def test_anomaly_title_is_what_on_asset_at_waypoint():
    t = T_titles.flag_title(ev(anomaly={"anomaly_type": "VISUAL_DEFECT", "severity": "HIGH"}), "equipment")
    assert t == "Visual defect on HX-208 (WP03)"


def test_failure_title_names_the_cause():
    t = T_titles.flag_title(ev(status="FAILED", failure_type="SENSOR_TIMEOUT"), "data_quality")
    assert t == "Gauge reading failed: sensor timeout on HX-208 (WP03)"


def test_battery_only_appears_when_it_is_the_problem():
    assert T_titles.flag_title(ev(battery_pct=15), "robot_health") == "Low battery (15%) at HX-208 (WP03)"
    assert "battery" not in T_titles.flag_title(ev(anomaly={"anomaly_type": "LEAK", "severity": "HIGH"}), "equipment")


def test_reading_titles():
    good_hot = {"measurement_type": "TEMPERATURE_C", "value": 120.0, "unit": "C", "quality": "GOOD"}
    assert T_titles.flag_title(ev(reading=good_hot), "equipment") == "Temperature out of safe band on HX-208 (WP03)"
    noisy = {"measurement_type": "PRESSURE_PSI", "value": 80.0, "unit": "psi", "quality": "QUESTIONABLE"}
    assert T_titles.flag_title(ev(reading=noisy), "data_quality") == "Untrusted pressure reading on HX-208 (WP03)"


def test_mission_titles():
    assert (T_titles.flag_title(MISSION, "mission")
            == "Mission M017 marked complete, but 3 of 10 actions failed and 1 serious anomaly")
    assert T_titles.flag_title(ev(MISSION, reported_status="FAILED"), "mission") == "Mission M017 failed (3 of 10 actions, 1 serious anomaly)"
    clean = ev(MISSION, actions_failed=0, anomalies_by_severity={"LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0})
    assert T_titles.flag_title(clean, "mission") == "Mission M017 completed"


def test_validator_uses_the_shared_title():
    decision = {"category": "robot_health"}
    assert V.flag_title("action", ev(battery_pct=12), decision, []) == "Low battery (12%) at HX-208 (WP03)"
    assert V.flag_title("mission", MISSION, {"category": "none"}).startswith("Mission M017 marked complete")
