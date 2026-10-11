import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vision.agreement import AGREE_FRAC, POLICY_VERSION, canon_unit, decide  # noqa: E402

REG = {"instrument_id": "PG-1", "unit": "bar", "min": 0, "max": 10}
OK_IMG = {"ok": True}


def A(v, unit="bar", **kw):
    return dict({"name": "eth_registered", "version": "518cdfa", "value": v, "unit": unit}, **kw)


def B(v, unit="bar", status="ok", **kw):
    return dict({"name": "qwen_registered", "version": "q8_0", "value": v, "unit": unit, "status": status}, **kw)


def test_agreement_uses_reader_a_value_and_needs_confirmation_by_default():
    d = decide(REG, OK_IMG, A(3.40), B(3.55))
    assert d["decision"] == "agreement_needs_confirmation" and d["policy_version"] == POLICY_VERSION
    assert d["final_value"] == 3.40 and d["final_source"] == "eth_registered" and d["final_unit"] == "bar"
    assert d["diff_frac_of_span"] == 0.015
    assert [r["value"] for r in d["readers"]] == [3.40, 3.55]          # both originals kept


def test_promoted_policy_accepts_and_boundary_is_inclusive():
    d = decide(REG, OK_IMG, A(3.0), B(3.0 + AGREE_FRAC * 10), automatic=True)
    assert d["decision"] == "agreement_accepted" and d["final_value"] == 3.0


def test_disagreement_is_never_averaged():
    d = decide(REG, OK_IMG, A(3.0), B(3.5))
    assert d["decision"] == "review_disagreement" and d["final_value"] is None


def test_invalid_outputs_request_recapture():
    for a, b, frag in ((A(None, error="Segmentation failed"), B(3.0), "reader failed"),
                       (A(3.0), B(3.0, unit="psi"), "registered"),
                       (A(3.0), B(3.0, status="ambiguous"), "status"),
                       (A(12.0), B(12.0), "outside the registered scale")):
        d = decide(REG, OK_IMG, a, b, automatic=True)
        assert d["decision"] == "recapture_or_review" and frag in d["reason"] and d["final_value"] is None


def test_bad_photo_requests_recapture_even_if_readers_agree():
    d = decide(REG, {"ok": False, "reason": "shorter side 120 px"}, A(3.0), B(3.0), automatic=True)
    assert d["decision"] == "recapture_or_review" and "120 px" in d["reason"]


def test_units_with_printed_multiplier():
    assert canon_unit("X10⁻¹MPa") == ("mpa", 0.1) and canon_unit("Kg/cm²")[0] == "kgf/cm2"
    reg = {"instrument_id": "EXT-1", "unit": "MPa", "min": 0, "max": 1.0}
    d = decide(reg, OK_IMG, A(0.95, unit="MPa"), B(9.6, unit="X10⁻¹MPa"))
    assert d["decision"] == "agreement_needs_confirmation"
