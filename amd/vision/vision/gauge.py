"""Dial gauge reader.

Hough circle (fallback: largest bright blob) -> needle = argmax of ray darkness from the centre, with the
second-peak/first-peak ratio as an ambiguity score -> OCR'd scale labels -> value = a*angle + b by RANSAC (rejects
mis-printed labels such as the kit gauge's '290') -> fallback hint range / default 270° sweep.

The kit's gauge_pressure_01.jpg is AI-generated with a two-ended needle: the right answer is LOW confidence +
AMBIGUOUS_NEEDLE, not a confident number.
"""
import math
from typing import List, Optional, Tuple

import cv2
import numpy as np

from . import textaxes
from .result import Frame, Mark, ReaderResult, Reading, clamp01, r1

AMBIGUITY_RATIO = 0.6        # second needle peak / first peak above this -> ambiguous
AMBIGUOUS_CAP = 0.3
UNITS = {"psi": "psi", "bar": "bar", "kpa": "kPa", "mpa": "MPa", "inhg": "inHg", "kg/cm2": "kg/cm²"}


def find_dial(gray: np.ndarray) -> Optional[Tuple[float, float, float]]:
    """(cx, cy, r) of the dial face in `gray` pixels."""
    h, w = gray.shape
    m = min(h, w)
    blur = cv2.medianBlur(gray, 5)
    circles = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp=1.2, minDist=m / 2, param1=120, param2=40,
                               minRadius=int(0.22 * m), maxRadius=int(0.55 * m))
    best = None
    if circles is not None:
        for cx, cy, r in circles[0]:
            # prefer a bright face (dial) near the image centre
            mask = np.zeros_like(gray)
            cv2.circle(mask, (int(cx), int(cy)), int(r * 0.8), 255, -1)
            face = float(gray[mask > 0].mean()) if mask.any() else 0.0
            off = math.hypot(cx - w / 2, cy - h / 2) / m
            score = face / 255.0 + r / m - off
            if best is None or score > best[0]:
                best = (score, (float(cx), float(cy), float(r)))
    if best is not None:
        return best[1]
    # fallback: largest bright blob's enclosing circle
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(c) < 0.05 * h * w:
        return None
    (cx, cy), r = cv2.minEnclosingCircle(c)
    return float(cx), float(cy), float(r)


def ray_profile(gray: np.ndarray, cx: float, cy: float, r: float, r0: float = 0.12, r1_: float = 0.72,
                step_deg: float = 1.0) -> Tuple[np.ndarray, np.ndarray]:
    """(angles cw-from-up in degrees, darkness 0..255) sampled along rays from r0*r to r1_*r."""
    angs = np.arange(-180.0, 180.0, step_deg)
    rad = np.deg2rad(angs)
    rs = np.linspace(r0 * r, r1_ * r, 40)
    xs = cx + np.outer(np.sin(rad), rs)
    ys = cy - np.outer(np.cos(rad), rs)
    vals = cv2.remap(gray.astype(np.float32), xs.astype(np.float32), ys.astype(np.float32), cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REPLICATE)
    dark = 255.0 - vals
    # a needle is dark along most of the ray: use a high quantile, not the mean (labels darken only part of a ray)
    prof = np.quantile(dark, 0.35, axis=1)
    return angs, prof


def needle_peaks(angs: np.ndarray, prof: np.ndarray, min_sep: float = 35.0):
    """[(angle, strength)] of the two strongest peaks at least min_sep degrees apart, strength above baseline."""
    k = 3
    sm = np.convolve(np.r_[prof[-k:], prof, prof[:k]], np.ones(2 * k + 1) / (2 * k + 1), mode="same")[k:-k]
    base = float(np.median(sm))
    s = sm - base
    i1 = int(np.argmax(s))
    d = np.abs((angs - angs[i1] + 180) % 360 - 180)
    s2 = np.where(d >= min_sep, s, -np.inf)
    i2 = int(np.argmax(s2))
    return [(float(angs[i1]), float(s[i1])), (float(angs[i2]), float(max(0.0, s2[i2])))]


def dark_run(gray: np.ndarray, cx: float, cy: float, r: float, ang: float, thr: float,
             start: float = 0.08, stop: float = 0.9, band: bool = False) -> float:
    """Length (fraction of r) of the contiguous dark run along a ray, starting just outside the pivot."""
    fs = np.arange(start, stop, 0.02)
    # band=True samples across the ray (the needle angle comes from a 1° grid, which at 0.7 r is ~2 px off a 3-4 px
    # needle) and keeps the darkest pixel of each band; used for the pointer length only, so the two-ended check
    # (tail) keeps its original single-ray behaviour
    sn, cs = math.sin(math.radians(ang)), math.cos(math.radians(ang))
    half = max(2.0, 0.015 * r) if band else 0.0
    offs = np.linspace(-half, half, 5 if band else 1)
    xs = np.clip(np.round(cx + np.outer(fs * r, [sn] * len(offs)) + np.outer(np.ones_like(fs), offs * cs)).astype(int),
                 0, gray.shape[1] - 1)
    ys = np.clip(np.round(cy - np.outer(fs * r, [cs] * len(offs)) + np.outer(np.ones_like(fs), offs * sn)).astype(int),
                 0, gray.shape[0] - 1)
    dark = (255.0 - gray[ys, xs].astype(np.float32)).max(axis=1) > thr
    n = 0
    while n < len(dark) and (dark[n] or (n + 1 < len(dark) and dark[n + 1])):   # tolerate 1-sample gaps
        n += 1
    return float(fs[n - 1] if n else 0.0)


def tail(gray: np.ndarray, cx: float, cy: float, r: float, a1: float) -> Tuple[float, float, float]:
    """(needle length, longest opposite-side run, its angle). Real counterweight tails stay short (< ~0.25 r);
    a long dark run on the opposite side means a two-ended needle."""
    angs, prof = ray_profile(gray, cx, cy, r, step_deg=1.0)
    face = float(np.median(prof))
    i1 = int(np.argmin(np.abs(angs - a1)))
    thr = face + 0.5 * (float(prof[i1]) - face)
    nlen = dark_run(gray, cx, cy, r, a1, thr)
    best = (0.0, a1 + 180.0)
    for da in range(-25, 26, 1):
        a = ((a1 + 180.0 + da + 180.0) % 360.0) - 180.0
        L = dark_run(gray, cx, cy, r, a, thr)
        if L > best[0]:
            best = (L, a)
    return nlen, best[0], best[1]


TAIL_MAX = 0.25


def _pointer_len(gray: np.ndarray, cx: float, cy: float, r: float, a1: float, angs, prof) -> float:
    """Length (fraction of r) of the followed arm, measured with band sampling (robust to the 1° angle grid)."""
    face = float(np.median(prof))
    i1 = int(np.argmin(np.abs(angs - a1)))
    return dark_run(gray, cx, cy, r, a1, face + 0.5 * (float(prof[i1]) - face), band=True)
# The pointer reaches toward the scale ring; a dark arm shorter than this is usually the counterweight (MeasureBench
# pressure_gauge_10/80: 0.48 r / 0.46 r arms read the counterweight). Chosen on the 118 development images: it keeps
# every HIGH reading that was correct; not yet validated on held-out images.
POINTER_MIN = 0.6


def _unit(words) -> Optional[str]:
    for w in words:
        t = w.text.lower().replace(" ", "")
        for k, v in UNITS.items():
            if t == k or t.endswith(k):
                return v
    return None


def read(frame: Frame, hints: Optional[dict] = None) -> ReaderResult:
    hints = hints or {}
    img = frame.work
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    res = ReaderResult()
    dial = find_dial(gray)
    if dial is None:
        res.flags.append("NO_DIAL")
        res.words = textaxes.ocr(frame.orig)
        return res
    cx, cy, r = dial
    words = textaxes.ocr(img, region=(cx - 1.05 * r, cy - 1.05 * r, cx + 1.05 * r, cy + 1.05 * r))
    # scale labels: numeric words in the outer ring
    ticks = []
    for w in words:
        rr = math.hypot(w.cx - cx, w.cy - cy) / r
        if not (0.35 <= rr <= 1.0) or w.conf < 0.5:
            continue
        v = textaxes.parse_num(w.text)
        if v is None or len(w.text) > 6:
            continue
        ticks.append(textaxes.Tick(textaxes.angle_cw_from_up(w.cx, w.cy, cx, cy), v, w))
    flags: List[str] = []
    conf = 0.85
    fit = None
    if len(ticks) >= 3:
        span = max(t.value for t in ticks) - min(t.value for t in ticks)
        fit = textaxes.calibrate_axis(ticks, "linear", residual=max(1e-6, 0.04 * span), expect_sign=1)
    angs, prof = ray_profile(gray, cx, cy, r)
    (a1, s1), (a2, s2) = needle_peaks(angs, prof)
    ratio = s2 / s1 if s1 > 0 else 1.0
    nlen, tlen, ta = tail(gray, cx, cy, r, a1)
    two_ended = tlen >= TAIL_MAX
    pointer_len = _pointer_len(gray, cx, cy, r, a1, angs, prof)
    if two_ended and tlen / max(nlen, 1e-6) > ratio:
        a2, ratio = ta, tlen / max(nlen, 1e-6)
    # No silent unit: 38 of 96 MeasureBench readings used to default to psi, 22 of them on gauges with no psi scale.
    # A registered instrument (hints) may supply the unit; otherwise the reading cannot be trusted.
    unit = _unit(words) or hints.get("unit")
    if unit is None:
        unit = "psi"                      # placeholder for the number's display only; flagged and capped below
        unit_unknown = True
    else:
        unit_unknown = False
    full_scale = None
    if fit is not None:
        inl = [t for t in ticks if t not in fit.rejected]
        full_scale = max(t.value for t in inl)
        lo_val = min(t.value for t in inl)
        if fit.rejected:
            flags.append("LABEL_OUTLIER")
        method = "hough_ray_ransac_labels"
        if fit.max_resid > 0.03 * max(1.0, full_scale - lo_val):
            conf -= 0.15
        value_at = lambda a: float(fit.value(a))  # noqa: E731
        amin, amax = float(fit.pos(lo_val)), float(fit.pos(full_scale))
        vals = sorted({t.value for t in inl})
        step = (amax - amin) / max(1, len(vals) - 1)          # one label step of slack (a needle can hide a label)
        amin, amax = amin - step, amax + step
    else:
        sweep = float(hints.get("sweep_deg") or 270.0)
        lo_val = float(hints.get("range_min") or 0.0)
        if hints.get("range_max") is not None:
            full_scale = float(hints["range_max"])
            flags.append("USED_HINT")
        elif ticks:
            full_scale = max(t.value for t in ticks)
        if full_scale is None or full_scale <= lo_val:
            res.flags.append("AXIS_FIT_FAILED")
            res.words = [w.shifted(0, 0, frame.scale) for w in words]
            res.extra_marks = [Mark("circle", [frame.to_orig(cx), frame.to_orig(cy), frame.to_orig(r)])]
            return res
        flags.append("DEFAULT_SWEEP")
        method = "hough_ray_default_sweep"
        conf = min(conf, 0.4)
        amin, amax = -sweep / 2, sweep / 2
        value_at = lambda a: lo_val + (a - amin) / (amax - amin) * (full_scale - lo_val)  # noqa: E731

    def in_sweep(a):
        lo, hi = min(amin, amax), max(amin, amax)
        return lo - 8 <= a <= hi + 8
    def clamp_small(v):
        if full_scale and lo_val >= 0 and -0.02 * full_scale <= v < 0:
            return 0.0
        return v
    v1 = clamp_small(value_at(a1))
    v2 = clamp_small(value_at(a2))
    if s1 < 12:                         # no dark needle stands out
        conf = min(conf, 0.3)
        flags.append("WEAK_NEEDLE")
    if not in_sweep(a1):
        conf = min(conf, 0.3)
        flags.append("NEEDLE_OUTSIDE_SCALE")
    if unit_unknown:
        conf = min(conf, 0.3)
        flags.append("UNIT_UNKNOWN")
    if pointer_len < POINTER_MIN:       # a counterweight, not a pointer that reaches the scale
        conf = min(conf, 0.3)
        flags.append("SHORT_NEEDLE")
    ambiguous = ratio > AMBIGUITY_RATIO or two_ended
    note = "needle at %.0f° reads %.1f %s" % (a1, v1, "(unit unknown)" if unit_unknown else unit)
    if ambiguous:
        conf = min(conf, AMBIGUOUS_CAP)
        flags.append("AMBIGUOUS_NEEDLE")
        why = ("two-ended needle (opposite arm %.2f r long)" % tlen) if two_ended else ("peak ratio %.2f" % ratio)
        note = ("two needle candidates: %.0f° -> %.0f %s and %.0f° -> %.0f %s (%s); do not trust this value"
                % (a1, v1, unit, a2, v2, unit, why))
    tip = (cx + 0.75 * r * math.sin(math.radians(a1)), cy - 0.75 * r * math.cos(math.radians(a1)))
    marks = [Mark("line", [frame.to_orig(cx), frame.to_orig(cy), frame.to_orig(tip[0]), frame.to_orig(tip[1])])]
    if ambiguous:
        tip2 = (cx + 0.75 * r * math.sin(math.radians(a2)), cy - 0.75 * r * math.cos(math.radians(a2)))
        marks.append(Mark("dashed", [frame.to_orig(cx), frame.to_orig(cy), frame.to_orig(tip2[0]),
                                     frame.to_orig(tip2[1])]))
    nd = 1 if full_scale is not None and full_scale <= 100 else 0
    res.readings = [Reading("pressure" if unit in ("psi", "bar", "kPa", "MPa") else "dial_value", r1(v1, nd), unit,
                            round(clamp01(conf), 2), method, flags,
                            point=frame.pt_to_orig(tip), note=note, primary=True, full_scale=full_scale,
                            marks=marks)]
    res.key_results = [{"name": "Reading", "units": unit, "value": r1(v1, nd)},
                       {"name": "Reading Confidence", "units": "%", "value": int(round(100 * clamp01(conf)))},
                       {"name": "Needle Angle", "units": "deg", "value": r1(a1)},
                       {"name": "Needle Peak Ratio", "units": "", "value": round(ratio, 2)},
                       {"name": "Opposite Arm Length", "units": "r", "value": round(tlen, 2)}]
    if full_scale is not None:
        res.key_results.append({"name": "Full Scale", "units": unit, "value": full_scale})
    if fit is not None:
        res.key_results.append({"name": "Scale Labels Used", "units": "", "value": "%d of %d" % (fit.inliers, len(ticks))})
    res.words = [w.shifted(0, 0, frame.scale) for w in words]
    res.extra_marks = [Mark("circle", [frame.to_orig(cx), frame.to_orig(cy), frame.to_orig(r)])]
    res.extra_marks += [Mark("xbox", list(frame.box_to_orig(t.word.bbox))) for t in (fit.rejected if fit else [])]
    return res
