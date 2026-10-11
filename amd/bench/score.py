"""Score /root/bench/run/<name>/results.jsonl -> summary.json + a printed table. Standard library only.
Accuracy-type rates carry a Wilson 95% interval; unparseable or failed calls count as wrong."""
import json, math, os, statistics as st, sys

R = "/root/bench/run"


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(100 * (c - h), 1), round(100 * (c + h), 1)]


def rate(k, n):
    return {"pct": round(100 * k / n, 1) if n else None, "k": k, "n": n, "ci95": wilson(k, n)}


def binary(pairs):
    """pairs of (truth: bool, pred: bool | None); positive = True. None (no answer) counts as wrong."""
    tp = sum(1 for t, p in pairs if t and p is True)
    fn = sum(1 for t, p in pairs if t and p is not True)
    tn = sum(1 for t, p in pairs if not t and p is False)
    fp = sum(1 for t, p in pairs if not t and p is not False)
    n = len(pairs)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    tpr = rec
    tnr = tn / (tn + fp) if tn + fp else 0.0
    return {"accuracy": rate(tp + tn, n), "balanced_accuracy_pct": round(50 * (tpr + tnr), 1),
            "precision_pct": round(100 * prec, 1), "recall_pct": round(100 * rec, 1), "f1_pct": round(100 * f1, 1),
            "false_positive_rate_pct": round(100 * fp / (fp + tn), 1) if fp + tn else None,
            "confusion": {"tp": tp, "fn": fn, "tn": tn, "fp": fp}}


def latency(rows):
    xs = sorted(r["latency_s"] for r in rows if "latency_s" in r)
    if not xs:
        return {}
    return {"p50_s": round(st.median(xs), 2), "p95_s": round(xs[min(len(xs) - 1, int(0.95 * len(xs)))], 2),
            "total_s": round(sum(xs), 1)}


def p(r, key):
    return (r.get("parsed") or {}).get(key) if isinstance(r.get("parsed"), dict) else None


def score(name, rows):
    out = {"n": len(rows), "parsed": rate(sum(1 for r in rows if isinstance(r.get("parsed"), dict)), len(rows)),
           "errors": sum(1 for r in rows if "error" in r), "latency": latency(rows)}
    if name == "visual":   # positive = "yes, the object is there"
        pairs = [(r["truth"]["answer"] == "yes", None if p(r, "answer") is None else p(r, "answer") == "yes") for r in rows]
        out["metrics"] = binary(pairs)
        out["metrics"]["yes_ratio_pct"] = round(100 * sum(1 for _, x in pairs if x) / len(pairs), 1)
        out["headline"] = "RePOPE adversarial: accuracy %s%%, F1 %s%%, false-yes rate %s%%" % (
            out["metrics"]["accuracy"]["pct"], out["metrics"]["f1_pct"], out["metrics"]["false_positive_rate_pct"])
    elif name == "navigation":
        maze = [r for r in rows if r["truth"]["task"] == "mazenav"]
        navi = [r for r in rows if r["truth"]["task"] == "navitrace"]
        out["maze_nav_accuracy"] = rate(sum(1 for r in maze if p(r, "answer") == r["truth"]["answer"]), len(maze))
        # positive = "a wheeled robot cannot do it" (the blocked path an operator must hear about)
        out["navitrace_blocked"] = binary([(not r["truth"]["wheeled_robot_ok"],
                                            None if p(r, "wheeled_robot_ok") is None else not p(r, "wheeled_robot_ok")) for r in navi])
        out["headline"] = "Maze-Nav route reading %s%% (chance 25%%); NaviTrace blocked-path balanced accuracy %s%%" % (
            out["maze_nav_accuracy"]["pct"], out["navitrace_blocked"]["balanced_accuracy_pct"])
    elif name == "thermal":   # positive = overheating
        out["metrics"] = binary([(r["truth"]["overheating"], None if p(r, "status") is None else p(r, "status") == "overheating") for r in rows])
        out["headline"] = "Motor overheating: accuracy %s%%, balanced %s%%, F1 %s%%" % (
            out["metrics"]["accuracy"]["pct"], out["metrics"]["balanced_accuracy_pct"], out["metrics"]["f1_pct"])
    elif name == "acoustic":   # positive = leak
        out["metrics"] = binary([(r["truth"]["leak"], None if p(r, "status") is None else p(r, "status") == "anomaly") for r in rows])
        for noise in sorted({r["truth"]["noise"] for r in rows}):
            sub = [r for r in rows if r["truth"]["noise"] == noise]
            out["by_noise_" + noise] = binary([(r["truth"]["leak"], None if p(r, "status") is None else p(r, "status") == "anomaly") for r in sub])["balanced_accuracy_pct"]
        errs = [abs(p(r, "peak_khz") - r["truth"]["peak_khz"]) for r in rows if isinstance(p(r, "peak_khz"), (int, float))]
        out["peak_khz_median_abs_error"] = round(st.median(errs), 2) if errs else None
        # code baseline: the best single threshold on the 15-20 kHz energy share (fit on these same clips: optimistic)
        best = 0
        for thr in sorted({r["truth"]["hf_frac"] for r in rows}):
            b = binary([(r["truth"]["leak"], r["truth"]["hf_frac"] >= thr) for r in rows])["balanced_accuracy_pct"]
            best = max(best, b)
        out["code_baseline_hf_frac_balanced_pct"] = best
        out["headline"] = "Air-leak spectrogram: accuracy %s%%, balanced %s%% (code baseline %s%%)" % (
            out["metrics"]["accuracy"]["pct"], out["metrics"]["balanced_accuracy_pct"], best)
    elif name == "gauge":
        errs = []
        for r in rows:
            v, t, rng = p(r, "value"), r["truth"]["reading"], r["truth"]["range"]
            errs.append(abs(v - t) / rng if isinstance(v, (int, float)) and rng else None)
        n = len(rows)
        for tol in (0.02, 0.05, 0.10):
            out["within_%d_pct_fs" % round(tol * 100)] = rate(sum(1 for e in errs if e is not None and e <= tol), n)
        valid = [e for e in errs if e is not None]
        out["median_abs_error_pct_fs"] = round(100 * st.median(valid), 1) if valid else None
        out["scale_max_correct"] = rate(sum(1 for r in rows if isinstance(p(r, "scale_max"), (int, float))
                                            and abs(p(r, "scale_max") - r["truth"]["range"]) <= 0.01 * r["truth"]["range"]), n)
        out["headline"] = "Substation dial reading: %s%% within ±2%% FS, %s%% within ±5%% FS (median error %s%% FS)" % (
            out["within_2_pct_fs"]["pct"], out["within_5_pct_fs"]["pct"], out["median_abs_error_pct_fs"])
    return out


summary = {}
for name in sys.argv[1:] or ("thermal", "gauge", "acoustic", "visual", "navigation"):
    path = os.path.join(R, name, "results.jsonl")
    if not os.path.exists(path):
        continue
    rows = list({r["id"]: r for r in (json.loads(l) for l in open(path))}.values())   # one row per item id
    if rows:
        summary[name] = score(name, rows)
        print("%-10s n=%-3d %s | parsed %s%% | p50 %ss" % (name, len(rows), summary[name]["headline"],
                                                     summary[name]["parsed"]["pct"], summary[name]["latency"].get("p50_s")))
json.dump(summary, open(os.path.join(R, "summary.json"), "w"), indent=2)
