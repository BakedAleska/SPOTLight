"""Gauge-reading evaluation on MeasureBench real_world pressure gauges: run our reader, and score any reader.

    python scripts/measurebench_eval.py run   --parquet real_world.parquet --out DIR [--ids a,b] [--save-images]
    python scripts/measurebench_eval.py score --parquet real_world.parquet --pred DIR/predictions.jsonl [--out FILE]

`run` calls vision.pipeline.analyze (the code behind POST /v1/vision) with kind=gauge on each image and writes
DIR/predictions.jsonl (one line per item: value, unit, confidence, flags, the trust the orchestrator would assign,
latency) plus, per item, the full response JSON, the original image and the annotated overlay (--save-images).

`score` reads any predictions.jsonl with {"question_id", "value", "unit", "accepted"} (value/unit null = no reading)
and scores it against MeasureBench's labels. Definitions (fixed before comparing readers):
- labels: `interval` [lo, hi] + `units`, or `intervals` + per-interval `units` (dual-scale gauges; a reading on
  either labelled scale counts, as each interval is a complete answer for its scale).
- unit_correct: the predicted unit is one of the label units of some interval (after canonicalising spellings).
- strict_correct: unit_correct and lo <= value <= hi on that same interval (MeasureBench's interval rule).
- tolerant_correct: unit_correct and the value is within that interval widened by TOL_FRAC of the interval's upper
  bound (a declared prototype tolerance, not MeasureBench's).
- value_correct_any_unit: the value converted to kPa lies in some interval converted to kPa (diagnoses unit errors).
- rel_error: |value - nearest interval bound| / |upper bound| on the best interval with the predicted unit
  (0 inside the interval); null when the unit matches no interval.
Accepted = whatever the reader's acceptance rule says (for ours: the orchestrator's HIGH trust). Coverage, accuracy
among accepted and wrong-accepted are reported separately; an abstention is never counted as correct.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOL_FRAC = 0.03
FORCE_LOW = {"AMBIGUOUS_NEEDLE", "DEFAULT_SWEEP"}   # orchestrator.assess.FORCE_LOW_FLAGS
TRUST_MIN = 0.5                                     # orchestrator VISION_TRUST_MIN default
TO_KPA = {"psi": 6.894757, "bar": 100.0, "mbar": 0.1, "hpa": 0.1, "kpa": 1.0, "mpa": 1000.0, "pa": 0.001,
          "cmhg": 1.33322, "cmh2o": 0.0980665, "mmh2o": 0.00980665,
          "kgf/cm2": 98.0665, "inhg": 3.38639, "mmhg": 0.133322, "inh2o": 0.249089, "mh2o": 9.80665, "atm": 101.325}
SPELLINGS = {
    "psi": ["psi", "pounds per square inch", "pound per square inch", "pound-force per square inch", "lb/in²",
            "lb/in2", "lbf/in2", "psig"],
    "bar": ["bar"], "mbar": ["mbar", "millibar"], "hpa": ["hpa", "hectopascal", "hectopascals"],
    "kpa": ["kpa", "kilopascal", "kilopascals"], "cmhg": ["cmhg", "cm hg"], "cmh2o": ["cmh2o", "cm h2o"],
    "mmh2o": ["mmh2o", "mm h2o"],
    "mpa": ["mpa", "megapascal", "megapascals"], "pa": ["pa", "pascal"],
    "kgf/cm2": ["kgf/cm^2", "kgf/cm²", "kg/cm^2", "kg/cm²", "kg/cm2", "kgf/cm2"],
    "inhg": ["inhg", "in hg", "inches of mercury", "in. hg", "inch hg"], "mmhg": ["mmhg", "mm hg", "millimeters of mercury"],
    "atm": ["atm"], "inh2o": ["inh2o", "in h2o", "inches of water", "in. h2o", "in wc", "inwc"],
}
CANON = {s: c for c, ss in SPELLINGS.items() for s in ss}


SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
MULT_RE = re.compile(r"^\s*[x×*]\s*10\s*\^?\s*(-?\d+)\s*|^\s*[x×*]\s*(\d*\.?\d+)\s*", re.IGNORECASE)


def split_multiplier(u):
    """("X10⁻¹MPa") -> ("MPa", 0.1): dials printed as 'x10^-1 MPa' or 'x0.1 MPa' scale the number."""
    if u is None:
        return None, 1.0
    s = str(u).translate(SUP)
    m = MULT_RE.match(s)
    if not m:
        return u, 1.0
    factor = 10.0 ** int(m.group(1)) if m.group(1) is not None else float(m.group(2))
    return s[m.end():].strip() or None, factor


def canon_unit(u):
    if u is None:
        return None
    t = re.sub(r"\s+", " ", str(u).strip().lower())
    return CANON.get(t, CANON.get(t.replace(" ", ""), t))


def label_intervals(kwargs: dict):
    """[(lo, hi, {canonical units})] for interval_matching / multi_interval_matching labels."""
    if "interval" in kwargs:
        pairs = [(kwargs["interval"], kwargs.get("units") or [])]
    else:
        pairs = list(zip(kwargs.get("intervals") or [], kwargs.get("units") or []))
    out = []
    for iv, units in pairs:
        units = units if isinstance(units, list) else [units]
        out.append((float(min(iv)), float(max(iv)), {canon_unit(u) for u in units}))
    return out


def score_one(kwargs: dict, value, unit) -> dict:
    ivs = label_intervals(kwargs)
    r = {"label_units": sorted({u for _, _, us in ivs for u in us}), "has_reading": value is not None,
         "unit_correct": False, "strict_correct": False, "tolerant_correct": False,
         "value_correct_any_unit": False, "number_correct_ignoring_unit": False, "rel_error": None}
    if value is None:
        return r
    unit, factor = split_multiplier(unit)
    value = value * factor
    u = canon_unit(unit)
    best = None
    for lo, hi, us in ivs:   # MeasureBench number_correct: the number alone, any interval, unit ignored
        tol = TOL_FRAC * max(abs(hi), abs(lo), 1e-9)
        r["number_correct_ignoring_unit"] |= lo - tol <= value <= hi + tol
    for lo, hi, us in ivs:
        if u in us or not us - {None}:   # no label units: any unit is accepted (MeasureBench unit_correct=None)
            r["unit_correct"] = True
            tol = TOL_FRAC * max(abs(hi), abs(lo), 1e-9)
            r["strict_correct"] |= lo <= value <= hi
            r["tolerant_correct"] |= lo - tol <= value <= hi + tol
            err = 0.0 if lo <= value <= hi else min(abs(value - lo), abs(value - hi)) / max(abs(hi), abs(lo), 1e-9)
            best = err if best is None else min(best, err)
        if u in TO_KPA:
            for lu in us:
                if lu in TO_KPA:
                    v = value * TO_KPA[u]
                    a, b = lo * TO_KPA[lu], hi * TO_KPA[lu]
                    tol = TOL_FRAC * max(abs(a), abs(b), 1e-9)
                    r["value_correct_any_unit"] |= a - tol <= v <= b + tol
    r["rel_error"] = best
    return r


FS_TOLS = (0.01, 0.02, 0.03)   # correctness within this fraction of the matching dial scale's span (reviewed items)


def fs_correct(kwargs: dict, value, unit, scales) -> dict:
    """{tol: bool}: unit matches a labelled interval AND |value - interval| <= tol * span of the reviewed dial scale
    in that unit (span = max - min from the human review). Gauge accuracy is specified as % of full scale."""
    out = {t: False for t in FS_TOLS}
    if value is None:
        return out
    unit, factor = split_multiplier(unit)
    value, u = value * factor, canon_unit(unit)
    for lo, hi, us in label_intervals(kwargs):
        if u not in us and us - {None}:
            continue
        span = next((s["max"] - s["min"] for s in scales if canon_unit(s["unit"]) == u
                     and s.get("max") is not None and s.get("min") is not None), None)
        if not span:
            continue
        d = 0.0 if lo <= value <= hi else min(abs(value - lo), abs(value - hi))
        for t in FS_TOLS:
            out[t] |= d <= t * span
    return out


def load_items(parquet: str):
    import pandas as pd
    df = pd.read_parquet(parquet)
    return df[df.image_type == "pressure_gauge"].reset_index(drop=True)


def cmd_run(a):
    from vision.config import Settings
    from vision.imageio import decode_bytes, downscale
    from vision.pipeline import analyze
    s = Settings()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    df = load_items(a.parquet)
    ids = set(a.ids.split(",")) if a.ids else None
    with open(out / "predictions.jsonl", "w", encoding="utf-8") as fh:
        for _, row in df.iterrows():
            qid = row.question_id
            if ids and qid not in ids:
                continue
            raw = row.image["bytes"]
            t0 = time.perf_counter()
            resp = analyze({"image_b64": base64.b64encode(raw).decode(), "kind": "gauge", "annotate": a.save_images,
                            "media_id": qid}, s)
            ms = int((time.perf_counter() - t0) * 1000)
            rd = (resp.get("readings") or [None])[0]
            flags = set((rd or {}).get("flags") or [])
            conf = (rd or {}).get("confidence")
            accepted = bool(rd) and conf is not None and conf >= TRUST_MIN and not flags & FORCE_LOW
            pred = {"question_id": qid, "value": (rd or {}).get("value"), "unit": (rd or {}).get("unit"),
                    "accepted": accepted, "confidence": conf, "flags": sorted(flags),
                    "response_flags": resp.get("flags"), "note": (rd or {}).get("note"), "latency_ms": ms,
                    "key_results": resp.get("key_results"), "version": resp.get("version")}
            fh.write(json.dumps(pred, ensure_ascii=False) + "\n")
            if a.save_images:
                d = out / "cases" / qid
                d.mkdir(parents=True, exist_ok=True)
                (d / "original.bin").write_bytes(raw)
                dec = decode_bytes(raw, s.max_pixels)
                work, scale = downscale(dec.rgb, s.work_max_side)
                from PIL import Image
                Image.fromarray(work).save(d / "processed_input.png")
                if resp.get("annotated_image_b64"):
                    (d / "overlay.jpg").write_bytes(base64.b64decode(resp["annotated_image_b64"]))
                (d / "response.json").write_text(json.dumps(dict(resp, annotated_image_b64=None, work_scale=scale),
                                                            indent=1, ensure_ascii=False), encoding="utf-8")
                (d / "label.json").write_text(json.dumps({"question_id": qid, "question": row.question,
                                                          "evaluator": row.evaluator,
                                                          "evaluator_kwargs": json.loads(row.evaluator_kwargs),
                                                          "meta_info": dict(row.meta_info or {})}, indent=1,
                                                         ensure_ascii=False), encoding="utf-8")
    print("wrote", out / "predictions.jsonl")


def summarize(scored):
    n = len(scored)
    acc = [s for s in scored if s["accepted"]]
    def k(rows, key):
        return sum(1 for s in rows if s[key])
    return {"n": n, "with_reading": k(scored, "has_reading"), "unit_correct": k(scored, "unit_correct"),
            "strict_correct_all": k(scored, "strict_correct"), "tolerant_correct_all": k(scored, "tolerant_correct"),
            "number_correct_ignoring_unit_all": k(scored, "number_correct_ignoring_unit"),
            "accepted": len(acc), "coverage": round(len(acc) / n, 3) if n else None,
            "accepted_tolerant_correct": k(acc, "tolerant_correct"), "accepted_strict_correct": k(acc, "strict_correct"),
            "accepted_wrong": len(acc) - k(acc, "tolerant_correct"),
            "abstained": n - len(acc),
            "abstained_but_reading_was_tolerant_correct": sum(1 for s in scored if not s["accepted"] and s["tolerant_correct"])}


def cmd_score(a):
    df = load_items(a.parquet)
    labels = {r.question_id: json.loads(r.evaluator_kwargs) for _, r in df.iterrows()}
    reviewed = {}
    if a.reviewed:
        reviewed = {json.loads(x)["q"]: json.loads(x) for x in open(a.reviewed, encoding="utf-8") if x.strip()}
    idmap = {}
    if a.map:
        idmap = {json.loads(x)["opaque_id"]: json.loads(x)["question_id"] for x in open(a.map, encoding="utf-8")}
    scored = []
    for line in open(a.pred, encoding="utf-8"):
        p = json.loads(line)
        qid = p.get("question_id") or idmap.get(p.get("opaque_id"))
        if qid not in labels:
            continue
        if reviewed and not reviewed[qid]["eval_clean"]:
            continue
        s = score_one(labels[qid], p.get("value"), p.get("unit"))
        if reviewed:
            for tol, ok in fs_correct(labels[qid], p.get("value"), p.get("unit"), reviewed[qid]["scales"]).items():
                s["fs%d_correct" % round(tol * 100)] = ok
        scored.append(dict(s, question_id=qid, accepted=bool(p.get("accepted")), value=p.get("value"),
                           unit=p.get("unit"), latency_ms=p.get("latency_ms")))
    summary = summarize(scored)
    if reviewed:
        acc = [s for s in scored if s["accepted"]]
        for tol in FS_TOLS:
            k = "fs%d_correct" % round(tol * 100)
            summary[k + "_all"] = sum(1 for s in scored if s[k])
            summary[k + "_accepted"] = sum(1 for s in acc if s[k])
            summary[k + "_accepted_wrong"] = len(acc) - summary[k + "_accepted"]
    print(json.dumps(summary, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps({"summary": summary, "tol_frac": TOL_FRAC, "cases": scored}, indent=1,
                                          ensure_ascii=False), encoding="utf-8")


def cmd_export(a):
    """Write each image as <opaque id>.<ext> (no question id, label or source in the name) + a private id map."""
    import hashlib
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    df = load_items(a.parquet)
    rows = []
    for i, row in df.iterrows():
        raw = row.image["bytes"]
        ext = "png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
        oid = "mb_%03d" % i
        (out / ("%s.%s" % (oid, ext))).write_bytes(raw)
        rows.append({"opaque_id": oid, "file": "%s.%s" % (oid, ext), "question_id": row.question_id,
                     "sha256": hashlib.sha256(raw).hexdigest()})
    Path(a.map).write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    print("exported", len(rows), "images to", out, "| id map (evaluator only):", a.map)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--parquet", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--map", required=True)
    r = sub.add_parser("run")
    r.add_argument("--parquet", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--ids")
    r.add_argument("--save-images", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("--parquet", required=True)
    s.add_argument("--pred", required=True)
    s.add_argument("--out")
    s.add_argument("--map", help="opaque_id -> question_id map from `export` (for readers that saw opaque names)")
    s.add_argument("--reviewed", help="reviewed_labels.jsonl: score only eval_clean items, add %%-of-scale correctness")
    a = ap.parse_args(argv)
    {"run": cmd_run, "score": cmd_score, "export": cmd_export}[a.cmd](a)


if __name__ == "__main__":
    main()
