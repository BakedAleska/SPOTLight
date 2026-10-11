"""Compare scored readers case by case (output of measurebench_eval.py score --out): who is right where, whether
agreement between readers predicts correctness, and results by label type and source.

    python scripts/compare_readers.py --parquet real_world.parquet NAME=score.json [NAME=score.json ...] --out FILE

Agreement = both readers produced a reading and the values agree within the same TOL_FRAC used for correctness,
after converting both to kPa when both units are known (else raw values). Agreement is a signal to evaluate, not
proof: two readers can agree on the same wrong scale.
"""
import argparse
import json
import re
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measurebench_eval import TO_KPA, TOL_FRAC, canon_unit, load_items  # noqa: E402


def as_kpa(v, u):
    c = canon_unit(u)
    return v * TO_KPA[c] if v is not None and c in TO_KPA else None


def agree(a, b):
    if a["value"] is None or b["value"] is None:
        return None
    ka, kb = as_kpa(a["value"], a["unit"]), as_kpa(b["value"], b["unit"])
    x, y = (ka, kb) if ka is not None and kb is not None else (a["value"], b["value"])
    return abs(x - y) <= TOL_FRAC * max(abs(x), abs(y), 1e-9)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", required=True)
    ap.add_argument("--out")
    ap.add_argument("runs", nargs="+")
    a = ap.parse_args(argv)
    df = load_items(a.parquet)
    meta = {}
    for _, r in df.iterrows():
        kw = json.loads(r.evaluator_kwargs)
        src = (r.meta_info or {}).get("source") or ""
        meta[r.question_id] = {"dual_scale": "intervals" in kw, "youtube": "youtu" in src,
                               "group": (re.search(r"(?:v=|youtu\.be/)([\w-]{6,})", src) or [None, src])[1]}
    runs = {}
    for spec in a.runs:
        name, path = spec.split("=", 1)
        runs[name] = {c["question_id"]: c for c in json.load(open(path, encoding="utf-8"))["cases"]}
    names = list(runs)
    qids = sorted(meta, key=lambda q: int(q.rsplit("_", 1)[1]))
    report = {"per_reader": {}, "oracle_any_correct": None, "pairs": {}, "by_category": {}, "cases": []}
    for n in names:
        rr = runs[n]
        report["per_reader"][n] = {"correct": sum(rr[q]["tolerant_correct"] for q in qids),
                                   "number_correct_ignoring_unit": sum(rr[q]["number_correct_ignoring_unit"] for q in qids),
                                   "with_reading": sum(rr[q]["has_reading"] for q in qids)}
    report["oracle_any_correct"] = sum(any(runs[n][q]["tolerant_correct"] for n in names) for q in qids)
    for x, y in combinations(names, 2):
        ag = [q for q in qids if agree(runs[x][q], runs[y][q])]
        both = [q for q in ag if runs[x][q]["tolerant_correct"] and runs[y][q]["tolerant_correct"]]
        report["pairs"]["%s+%s" % (x, y)] = {"agree": len(ag), "agree_and_both_correct": len(both),
                                              "agree_but_wrong": len(ag) - len(both),
                                              "only_%s_correct" % x: sum(runs[x][q]["tolerant_correct"] and not runs[y][q]["tolerant_correct"] for q in qids),
                                              "only_%s_correct" % y: sum(runs[y][q]["tolerant_correct"] and not runs[x][q]["tolerant_correct"] for q in qids)}
    for cat, fn in (("dual_scale", lambda q: meta[q]["dual_scale"]), ("single_scale", lambda q: not meta[q]["dual_scale"]),
                    ("youtube", lambda q: meta[q]["youtube"]), ("not_youtube", lambda q: not meta[q]["youtube"])):
        qs = [q for q in qids if fn(q)]
        report["by_category"][cat] = {"n": len(qs), **{n: sum(runs[n][q]["tolerant_correct"] for q in qs) for n in names}}
    for q in qids:
        report["cases"].append({"question_id": q, **meta[q], **{n: {"value": runs[n][q]["value"], "unit": runs[n][q]["unit"],
                                                                    "correct": runs[n][q]["tolerant_correct"]} for n in names}})
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
