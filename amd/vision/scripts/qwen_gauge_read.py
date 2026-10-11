"""Diagnostic only: ask a llama-server VLM to read gauge images directly (NOT the operator app's evidence path).

    python scripts/qwen_gauge_read.py --images DIR --out DIR --url http://127.0.0.1:8080 --model qwen3.8-27b-q8_0 \
        [--variant full|dial_crop] [--thinking]

Inputs are the image files only (opaque names such as mb_000.jpg); no label, unit, range, packet value or source is
sent. `dial_crop` crops to the dial found by the vision service's Hough detector (vision.gauge.find_dial) with a
15 % margin; if no dial is found the full image is sent and `crop` is recorded as null. Writes predictions.jsonl
(opaque_id, value, unit, scale, status, accepted = status == "ok", latency, raw text, parse error) and run.json
(prompt, schema, generation settings, server props). Score with measurebench_eval.py score --map.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROMPT = ("The image shows an analog gauge. Read the value the needle (pointer) indicates on the dial's printed "
          "scale. If the dial has more than one scale, choose one scale, report the reading on it and give that "
          "scale's unit as printed on the dial. Use status \"ok\" only if you can read the value; use \"ambiguous\" "
          "if the needle position or which scale applies is unclear, and \"unreadable\" if the dial cannot be read "
          "(blur, glare, occlusion, too small). Do not guess a unit that is not printed on the dial.")
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["status", "reading", "unit", "scale", "reason"],
          "properties": {"status": {"enum": ["ok", "ambiguous", "unreadable"]},
                         "reading": {"type": ["number", "null"]},
                         "unit": {"type": ["string", "null"], "maxLength": 24},
                         "scale": {"type": ["string", "null"], "maxLength": 80},
                         "reason": {"type": "string", "maxLength": 200}}}
MAX_TOKENS = 300


def crop_to_dial(raw: bytes):
    import numpy as np
    from PIL import Image
    import cv2
    from vision import gauge
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    rgb = np.asarray(im)
    dial = gauge.find_dial(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
    if dial is None:
        return raw, None
    cx, cy, r = dial
    m = 1.15 * r
    box = [max(0, int(cx - m)), max(0, int(cy - m)), min(rgb.shape[1], int(cx + m)), min(rgb.shape[0], int(cy + m))]
    buf = io.BytesIO()
    im.crop(box).save(buf, "PNG")
    return buf.getvalue(), box


REGISTERED = ("Registered instrument data for this gauge (from the facility's asset register, not a reading): "
              "printed scale(s): {scales}. Use this to identify the scale and unit; read the needle position yourself.")


def registered_text(scales) -> str:
    return "; ".join("%s from %g to %g" % (s["unit"], s["min"], s["max"]) for s in scales
                     if s.get("min") is not None and s.get("max") is not None)


def call(url, model, image: bytes, thinking: bool, extra: str = ""):
    mime = "image/png" if image[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    body = {"model": model, "temperature": 0, "max_tokens": MAX_TOKENS if not thinking else 4096, "stream": False,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, base64.b64encode(image).decode())}},
                {"type": "text", "text": PROMPT + ((" " + extra) if extra else "")}]}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "gauge_reading", "schema": SCHEMA}},
            "chat_template_kwargs": {"enable_thinking": thinking}}
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.load(r)
    return data, int((time.perf_counter() - t0) * 1000)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--images", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--model", default="qwen3.8-27b-q8_0")
    ap.add_argument("--variant", choices=("full", "dial_crop"), default="full")
    ap.add_argument("--thinking", action="store_true")
    ap.add_argument("--registered", help="reviewed_labels.jsonl + --map: add each gauge's registered scale ranges "
                                         "(unit, min, max; never a reading) to the prompt; only eval_clean items run")
    ap.add_argument("--map", help="opaque_id -> question_id map (needed with --registered)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    props = {}
    try:
        with urllib.request.urlopen(a.url.rstrip("/") + "/props", timeout=10) as r:
            props = json.load(r)
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        props = {"error": str(exc)}
    (out / "run.json").write_text(json.dumps({"prompt": PROMPT, "registered_template": REGISTERED if a.registered else None,
                                              "schema": SCHEMA, "variant": a.variant,
                                              "thinking": a.thinking, "temperature": 0, "max_tokens": MAX_TOKENS,
                                              "model": a.model, "server_props": {k: props.get(k) for k in (
                                                  "model_path", "build_info", "modalities", "n_ctx",
                                                  "default_generation_settings") if k in props}},
                                             indent=1, default=str), encoding="utf-8")
    reg = {}
    if a.registered:
        qmap = {json.loads(x)["opaque_id"]: json.loads(x)["question_id"] for x in open(a.map, encoding="utf-8")}
        rev = {json.loads(x)["q"]: json.loads(x) for x in open(a.registered, encoding="utf-8") if x.strip()}
        reg = {oid: registered_text(rev[q]["scales"]) for oid, q in qmap.items() if rev[q]["eval_clean"]}
    with open(out / "predictions.jsonl", "w", encoding="utf-8") as fh:
        for path in sorted(Path(a.images).iterdir()):
            if a.registered and path.stem not in reg:
                continue
            raw = path.read_bytes()
            img, box = crop_to_dial(raw) if a.variant == "dial_crop" else (raw, None)
            rec = {"opaque_id": path.stem, "variant": a.variant, "crop": box}
            try:
                extra = REGISTERED.format(scales=reg[path.stem]) if a.registered else ""
                rec["registered"] = reg.get(path.stem)
                data, ms = call(a.url, a.model, img, a.thinking, extra)
                msg = data["choices"][0]["message"]
                text = msg.get("content") or ""
                rec.update(latency_ms=ms, raw=text, reasoning_chars=len(msg.get("reasoning_content") or ""),
                           finish_reason=data["choices"][0].get("finish_reason"),
                           prompt_tokens=data.get("usage", {}).get("prompt_tokens"),
                           completion_tokens=data.get("usage", {}).get("completion_tokens"))
                try:
                    o = json.loads(text)
                    rec.update(status=o.get("status"), value=o.get("reading"), unit=o.get("unit"),
                               scale=o.get("scale"), reason=o.get("reason"), parse_error=None)
                except ValueError as exc:
                    rec.update(status=None, value=None, unit=None, parse_error=str(exc)[:200])
            except Exception as exc:  # noqa: BLE001
                rec.update(status=None, value=None, unit=None, error="%s: %s" % (type(exc).__name__, str(exc)[:200]))
            rec["accepted"] = rec.get("status") == "ok" and rec.get("value") is not None
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            print("%s %-10s %s %s %s ms" % (path.stem, rec.get("status"), rec.get("value"), rec.get("unit"),
                                            rec.get("latency_ms")), flush=True)


if __name__ == "__main__":
    main()
