"""Real-image smoke: kit photo -> vision /v1/vision -> orchestrator row builders -> System 2 /v1/assess (HTTP)."""
import copy, json, sys, time, urllib.request
sys.path.insert(0, "/root/SpotTheDOT/amd/orchestrator")
from orchestrator.assess import demo_image, vision_rows, ocr_lines, system2_image, reasons

KIT = "/root/SpotTheDOT/web/public/kit/inspection"
VKEY = open("/root/tmp/vision.key").read().strip()
SKEY = open("/root/tmp/s2.key").read().strip()

def post(url, body, key, timeout=60):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", "X-API-Key": key})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r), dict(r.headers), int((time.perf_counter() - t0) * 1000)

def load_request(case):
    if case == "gauge":
        return json.load(open("/root/SpotTheDOT/amd/system2/fixtures/backend/data_quality.json"))["request"], "GAUGE_READING"
    d = json.load(open("/root/SpotTheDOT/amd/system2/data/heldout_packets.json"))["cases"]
    return next(c for c in d if c["case_id"] == "heldout-A0118")["request"], "THERMAL_INSPECTION"

case, run = sys.argv[1], sys.argv[2]
req, action_type = load_request(case)
image, kind, media_id = demo_image(action_type, KIT)            # same pick as the orchestrator with DEMO_IMAGES=1
vbody = {"request_id": "realimg-%s-%s" % (case, run), "media_id": media_id, "kind": kind, "action_type": action_type,
         "image_b64": image, "annotate": True}                     # demo pairing: no `logged`, as in assess.py
vresp, _, vms = post("http://127.0.0.1:8084/v1/vision", vbody, VKEY)
rows = vision_rows(vresp, "demo", 0.5, None)
ocr = ocr_lines(vresp, 8)
s2_img, note = system2_image(vresp.get("annotated_image_b64"))
print("== %s run %s | photo %s.jpg | vision %d ms | kind %s | router %s" % (case, run, media_id, vms, vresp.get("kind"), vresp.get("router")))
for r in rows: print("   row", json.dumps(r))
print("   ocr", [o["text"] for o in ocr])
print("   alert", vresp.get("alert"), "| image to System 2:", note, "%d b64 chars" % len(s2_img or ""))

ctx = copy.deepcopy(req["context"]); ctx.pop("vision", None); ctx.pop("ocr", None)
if rows: ctx["vision"] = rows
if ocr: ctx["ocr"] = ocr
flag = req.get("flag") or {"id": "flag:%s" % req["flag_id"]}
body = {"flag_id": req["flag_id"], "request_id": vbody["request_id"], "flag": flag, "event": None,
        "reasons": [r.get("detail") if isinstance(r.get("detail"), str) else None for r in reasons(flag)], "context": ctx}
if s2_img: body["image_b64"] = s2_img
json.dump(body, open("/root/tmp/realimg-%s-body.json" % case, "w"))
out, hdr, ms = post("http://127.0.0.1:8083/v1/assess", body, SKEY, timeout=60)
h = {k.lower(): v for k, v in hdr.items()}
cited = [i for e in out.get("evidence", []) for i in e["ids"]]
print("   SYSTEM2 %d ms wall | cache %s | image %s | path %s | latency_ms %s | action %s" % (
      ms, h.get("x-system2-cache"), h.get("x-system2-image"), out.get("path"), out.get("latency_ms"), out.get("recommended_action")))
print("   cited", [e["ids"] for e in out.get("evidence", [])], "| V# cited:", [i for i in cited if i.startswith("V")])
for e in out.get("evidence", []): print("   claim", e["ids"], "|", e["claim"])
print("   answer:", out.get("answer"))
