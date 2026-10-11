"""Two-reader agreement policy for a linked gauge photo (candidate `gauge-agreement-v1`, frozen 2026-10-11).

Reader A is a geometric reader (needle segmentation + scale start/end keypoints, with the scale range taken from the
registered instrument record); reader B is a VLM reading the photo with the same registered record in its prompt.
Neither sees an evaluation answer. Both outputs are kept.

    decide(registered, image_check, reader_a, reader_b, automatic=False) -> dict

Decision values (the operator screen keys off these):
- `agreement_accepted`      both valid, |a - b| <= AGREE_FRAC * span, and the policy is promoted (`automatic=True`)
- `agreement_needs_confirmation`  same evidence, policy not promoted: an operator must confirm before use
- `review_disagreement`     both valid but they disagree: show both, request manual reading / review
- `recapture_or_review`     the photo or a reader output failed validity: request another capture or manual reading
The final measurement, when they agree, is reader A's value (geometric and drawn as an overlay the operator can
check); it is never the average and never chosen by closeness to anything. Agreement is supporting evidence, not
verification: both readers can make the same mistake.
"""
from __future__ import annotations

import math
import re
from typing import Optional

POLICY_VERSION = "gauge-agreement-v1"
AGREE_FRAC = 0.02          # of the registered scale span; fixed before the fresh evaluation, not tuned on it
RANGE_MARGIN_FRAC = 0.05   # a reading more than 5 % of span outside the registered scale is invalid
MIN_IMAGE_SIDE = 256       # px, shorter side of the decoded photo

_SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
_MULT = re.compile(r"^\s*[x×*]\s*10\s*\^?\s*(-?\d+)\s*|^\s*[x×*]\s*(\d*\.?\d+)\s*", re.IGNORECASE)
_SPELL = {
    "psi": ("psi", "psig", "lb/in2", "lb/in²", "lbf/in2", "pounds per square inch", "pound per square inch"),
    "bar": ("bar",), "mbar": ("mbar", "millibar"), "kpa": ("kpa", "kilopascal", "kilopascals"),
    "mpa": ("mpa", "megapascal", "megapascals"), "hpa": ("hpa",), "pa": ("pa",),
    "kgf/cm2": ("kgf/cm2", "kgf/cm²", "kg/cm2", "kg/cm²", "kgf/cm^2", "kg/cm^2"),
    "inhg": ("inhg", "in hg", "in. hg"), "mmhg": ("mmhg", "mm hg"), "inh2o": ("inh2o", "in h2o", "inches of water"),
    "mmh2o": ("mmh2o", "mm h2o"), "atm": ("atm",),
}
_CANON = {s: c for c, ss in _SPELL.items() for s in ss}


def canon_unit(unit: Optional[str]):
    """(canonical unit or None, multiplier) for spellings such as 'X10⁻¹MPa' -> ('mpa', 0.1)."""
    if unit is None:
        return None, 1.0
    s = str(unit).translate(_SUP)
    factor = 1.0
    m = _MULT.match(s)
    if m:
        factor = 10.0 ** int(m.group(1)) if m.group(1) is not None else float(m.group(2))
        s = s[m.end():]
    t = re.sub(r"\s+", " ", s.strip().lower())
    return _CANON.get(t, _CANON.get(t.replace(" ", ""), t or None)), factor


def _reader_check(r: dict, reg: dict) -> dict:
    """Validity of one reader output against the registered scale; returns {ok, value, reasons}."""
    reasons = []
    if r.get("error"):
        reasons.append("reader failed: %s" % str(r["error"])[:120])
    if r.get("status") not in (None, "ok"):
        reasons.append("reader status %s" % r.get("status"))
    v = r.get("value")
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
        reasons.append("no numeric reading")
        return {"ok": False, "value": None, "reasons": reasons}
    u, f = canon_unit(r.get("unit"))
    ru, _ = canon_unit(reg["unit"])
    if u != ru:
        reasons.append("unit %r is not the registered %r" % (r.get("unit"), reg["unit"]))
    value = float(v) * f
    span = float(reg["max"]) - float(reg["min"])
    lo, hi = float(reg["min"]) - RANGE_MARGIN_FRAC * span, float(reg["max"]) + RANGE_MARGIN_FRAC * span
    if not lo <= value <= hi:
        reasons.append("reading %g outside the registered scale %g-%g" % (value, reg["min"], reg["max"]))
    return {"ok": not reasons, "value": value, "reasons": reasons}


def decide(registered: dict, image_check: dict, reader_a: dict, reader_b: dict, automatic: bool = False) -> dict:
    """registered: {instrument_id, unit, min, max}; image_check: {ok, reason}; reader_*: {name, version, value, unit,
    status?, error?, ...}. Returns the evidence record fields for this policy."""
    span = float(registered["max"]) - float(registered["min"])
    a, b = _reader_check(reader_a, registered), _reader_check(reader_b, registered)
    rec = {"policy_version": POLICY_VERSION, "agree_frac": AGREE_FRAC, "registered": dict(registered),
           "image_check": dict(image_check), "readers": [dict(reader_a, check=a), dict(reader_b, check=b)],
           "diff_frac_of_span": None, "final_value": None, "final_unit": None, "final_source": None}
    if span <= 0:
        return dict(rec, decision="recapture_or_review", reason="registered scale has no span")
    if not image_check.get("ok"):
        return dict(rec, decision="recapture_or_review", reason="photo failed input checks: %s" % image_check.get("reason"))
    if not (a["ok"] and b["ok"]):
        why = []
        for r, c, default in ((reader_a, a, "A"), (reader_b, b, "B")):
            if not c["ok"]:
                why.append("%s: %s" % (r.get("name", default), ", ".join(c["reasons"])))
        return dict(rec, decision="recapture_or_review", reason="; ".join(why))
    diff = abs(a["value"] - b["value"]) / span
    rec["diff_frac_of_span"] = round(diff, 4)
    if diff > AGREE_FRAC + 1e-9:            # inclusive limit; epsilon only absorbs float rounding
        return dict(rec, decision="review_disagreement",
                    reason="readers differ by %.1f %% of the scale span (limit %.0f %%)" % (100 * diff, 100 * AGREE_FRAC))
    rec.update(final_value=a["value"], final_unit=registered["unit"], final_source=reader_a.get("name", "A"))
    if automatic:
        return dict(rec, decision="agreement_accepted",
                    reason="both readers agree within %.0f %% of span; policy promoted" % (100 * AGREE_FRAC))
    return dict(rec, decision="agreement_needs_confirmation",
                reason="both readers agree within %.0f %% of span; policy not promoted, operator confirmation required"
                       % (100 * AGREE_FRAC))
