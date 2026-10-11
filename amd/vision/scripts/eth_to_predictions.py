"""Convert an ethz-asl/analog_gauge_reader run folder (pipeline.py --eval) into predictions.jsonl for
measurebench_eval.py score --map.

    python scripts/eth_to_predictions.py RUN_DIR OUT.jsonl

Per image: value/unit from result.json ("Failed" -> no reading), the pipeline's own stage-quality numbers from
error.json (ellipse fit error, OCR lack of confidence, needle residual variance), and the stage that failed when no
reading came out. `accepted` here = a numeric reading with a unit found by OCR; it is NOT a calibrated acceptance
rule (choose that on validation data).
"""
import json
import re
import sys
from pathlib import Path


def main(run_dir: str, out: str) -> None:
    rows = []
    log = (Path(run_dir) / "run.log").read_text(encoding="utf-8", errors="replace").splitlines()
    for d in sorted(p for p in Path(run_dir).iterdir() if p.is_dir()):
        oid = d.name.rsplit(".", 1)[0]
        res = json.loads((d / "result.json").read_text()) if (d / "result.json").exists() else None
        err = json.loads((d / "error.json").read_text()) if (d / "error.json").exists() else {}
        first = (res or [{}])[0]
        value = first.get("reading")
        value = float(value) if isinstance(value, (int, float)) else None
        unit = first.get("unit") if value is not None else None
        failure = None
        if value is None:   # last ERROR line logged while this image was being processed
            start = next((i for i, x in enumerate(log) if x.endswith("/" + d.name)), None)
            if start is not None:
                nxt = next((i for i in range(start + 1, len(log)) if "Start processing image" in log[i]), len(log))
                errs = [x for x in log[start:nxt] if " - ERROR - " in x]
                failure = re.sub(r"^.* - ERROR - ", "", errs[-1])[:200] if errs else "no result"
        rows.append({"opaque_id": oid, "value": value, "unit": unit, "accepted": value is not None and bool(unit),
                     "stage_errors": err, "failure": failure})
    Path(out).write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    print("wrote", len(rows), "rows to", out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
