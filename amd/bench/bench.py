"""Run benchmark manifests against llama-server on the box (127.0.0.1:8080): one image per call, temperature 0,
thinking off, the item's JSON schema as response_format. Resumable: results.jsonl keeps every finished id.
Usage: bench.py <name> [<name> ...] [--budget-s SECONDS per benchmark]"""
import base64, json, os, sys, time, urllib.request

URL = os.environ.get("LLAMA_URL", "http://127.0.0.1:8080") + "/v1/chat/completions"
R = "/root/bench/run"
args = sys.argv[1:]
budget = float(args[args.index("--budget-s") + 1]) if "--budget-s" in args else 1e9
names = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] != "--budget-s")]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # loopback: never through the egress proxy


def call(item):
    b64 = base64.b64encode(open(item["image"], "rb").read()).decode()
    msgs = ([{"role": "system", "content": item["system"]}] if item.get("system") else []) + [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        {"type": "text", "text": item["prompt"]}]}]
    body = {"messages": msgs, "temperature": 0, "max_tokens": item.get("max_tokens", 64), "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": item["schema"]}}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.time()
    with opener.open(req, timeout=180) as r:
        out = json.load(r)
    choice = out["choices"][0]
    text = choice["message"].get("content") or ""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    return {"text": text, "parsed": parsed, "finish": choice.get("finish_reason"), "latency_s": round(time.time() - t0, 2),
            "usage": out.get("usage"), "timings": {k: out.get("timings", {}).get(k) for k in ("prompt_n", "predicted_n", "predicted_per_second")}}


for name in names:
    d = os.path.join(R, name)
    items = [json.loads(l) for l in open(os.path.join(d, "manifest.jsonl"))]
    done = set()
    res_path = os.path.join(d, "results.jsonl")
    if os.path.exists(res_path):
        done = {json.loads(l)["id"] for l in open(res_path)}
    t_start = time.time()
    with open(res_path, "a") as out:
        for item in items:
            if item["id"] in done:
                continue
            if time.time() - t_start > budget:
                print(name, "budget reached", flush=True); break
            try:
                res = call(item)
            except Exception as exc:
                res = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
            out.write(json.dumps({"id": item["id"], "truth": item["truth"], **res}) + "\n"); out.flush()
            print(name, item["id"], res.get("parsed"), res.get("latency_s"), res.get("error", ""), flush=True)
    print(name, "finished in %.0fs" % (time.time() - t_start), flush=True)
