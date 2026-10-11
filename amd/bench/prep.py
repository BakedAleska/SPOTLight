"""Build quick benchmark slices on the AMD box: /root/bench/run/<name>/{manifest.jsonl, img/*.jpg}.

One manifest line = one model call: {id, image, system?, prompt, schema, max_tokens, truth}.
Slices are small and seeded (BENCH_N per benchmark, balanced where the labels allow).
"""
import hashlib, io, json, os, random, sys, urllib.request, zipfile
import duckdb
import numpy as np
from PIL import Image

D, R = "/root/bench/data", "/root/bench/run"
N = int(os.environ.get("BENCH_N", "40"))
ONLY = set(sys.argv[1:])


def key(*parts):   # a stable id from the source row, so a larger slice resumes instead of re-running
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def save(img, path, max_side=1024):
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        s = max_side / max(w, h)
        img = img.resize((round(w * s), round(h * s)), Image.LANCZOS)
    img.save(path, "JPEG", quality=90)


def write(name, items):
    with open(os.path.join(R, name, "manifest.jsonl"), "w") as f:
        for it in items:
            f.write(json.dumps(it) + "\n")
    print(name, len(items), "items", flush=True)


def obj(props, required=None):
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


def balanced(rows, key, n, seed=0):
    random.Random(seed).shuffle(rows)
    groups = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    per = n // len(groups)
    return [r for g in sorted(groups) for r in groups[g][:per]]


def visual():   # RePOPE adversarial: object hallucination (yes/no)
    os.makedirs(R + "/visual/img", exist_ok=True)
    rows = duckdb.sql("select image_id, question, answer, image.bytes from read_parquet('%s/RePOPE/*.parquet') "
                      "where category = 'adversarial'" % D).fetchall()
    items = []
    for i, (iid, q, a, b) in enumerate(balanced(rows, lambda r: r[2], N)):
        i = key(iid, q)
        p = "%s/visual/img/%s.jpg" % (R, i)
        save(Image.open(io.BytesIO(b)), p)
        items.append({"id": "repope-%s" % i, "image": p, "prompt": "%s Answer yes or no only." % q,
                      "schema": obj({"answer": {"type": "string", "enum": ["yes", "no"]}}), "max_tokens": 16,
                      "truth": {"answer": a, "image_id": iid}})
    write("visual", items)


def navigation():   # SpatialEval Maze-Nav (route reading, MCQ) + NaviTrace (can a wheeled robot do it?)
    os.makedirs(R + "/navigation/img", exist_ok=True)
    rows = duckdb.sql("select id, text, oracle_option, image.bytes from read_parquet('%s/SpatialEval/vqa/*.parquet') "
                      "where id like 'mazenav%%'" % D).fetchall()
    random.Random(0).shuffle(rows)
    items = []
    for i, (rid, text, opt, b) in enumerate(rows[:N // 2]):
        i = key(rid)
        p = "%s/navigation/img/maze-%s.jpg" % (R, i)
        save(Image.open(io.BytesIO(b)), p)
        items.append({"id": "maze-%s" % i, "image": p, "prompt": text + "\nAnswer with the option letter only.",
                      "schema": obj({"answer": {"type": "string", "enum": ["A", "B", "C", "D"]}}), "max_tokens": 16,
                      "truth": {"answer": opt, "task": "mazenav"}})
    rows = duckdb.sql("select sample_id, task, list_contains(embodiments, 'Wheeled Robot'), category, image.bytes "
                      "from read_parquet('%s/navitrace/data/*.parquet')" % D).fetchall()
    for i, (sid, task, wheeled, cat, b) in enumerate(balanced(rows, lambda r: r[2], N // 2)):
        i = key(sid)
        p = "%s/navigation/img/navi-%s.jpg" % (R, i)
        save(Image.open(io.BytesIO(b)), p)
        prompt = ("You are the forward camera of a wheeled inspection robot. It cannot climb stairs or curbs and "
                  "cannot cross rough or blocked ground. Task: \"%s\". Can the wheeled robot complete this task along a "
                  "safe path from here?" % task)
        items.append({"id": "navi-%s" % i, "image": p, "prompt": prompt, "max_tokens": 64,
                      "schema": obj({"path_status": {"type": "string", "enum": ["clear", "blocked"]},
                                     "blocking_feature": {"type": "string", "enum": ["stairs", "curb", "door", "obstacle", "terrain", "none"]},
                                     "wheeled_robot_ok": {"type": "boolean"},
                                     "confidence": {"type": "number"}}),
                      "truth": {"wheeled_robot_ok": bool(wheeled), "task": "navitrace", "category": cat}})
    write("navigation", items)


def acoustic():   # RMIS iica: compressed-air leak vs no leak, rendered as a kit-style 0-20 kHz spectrogram
    import soundfile as sf
    from scipy import signal
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    os.makedirs(R + "/acoustic/img", exist_ok=True)
    rows = duckdb.sql("select file_name, leak, noise, status, audio.bytes from read_parquet('%s/RMIS/iica/*.parquet')" % D).fetchall()
    sos = signal.butter(4, 100, "highpass", fs=48000, output="sos")
    items = []
    for i, (fn, leak, noise, status, b) in enumerate(balanced(rows, lambda r: (r[1], r[3]), N)):
        x, sr = sf.read(io.BytesIO(b), always_2d=True)
        x = signal.sosfilt(sos, x[:, 0])
        f, t, S = signal.spectrogram(x, sr, window="hann", nperseg=2048, noverlap=1024)
        keep = f <= 20000
        db = 10 * np.log10(S[keep] + 1e-12)
        db = np.clip(db, db.max() - 70, None)
        fig, ax = plt.subplots(figsize=(6.4, 4), dpi=100)
        ax.pcolormesh(t, f[keep] / 1000, db, cmap="jet", shading="auto")
        ax.set_xlabel("Time (s)"); ax.set_ylabel("Frequency (kHz)"); ax.set_yticks([0, 5, 10, 15, 20])
        fig.tight_layout()
        buf = io.BytesIO(); fig.savefig(buf, format="png"); plt.close(fig)
        i = key(fn)
        p = "%s/acoustic/img/%s.jpg" % (R, i)
        save(Image.open(buf), p)
        psd = S[keep].mean(axis=1)
        items.append({"id": "iica-%s" % i, "image": p, "max_tokens": 96,
                      "system": "Inspection-robot spectrogram. x = time (s), y = 0-20 kHz, jet colormap (blue = low, red = "
                                "high energy), no colorbar: colour is relative only.",
                      "prompt": "A 10 s acoustic recording taken next to compressed-air piping. Is there an air leak "
                                "(broadband high-frequency hiss) or only normal background noise? Reply with JSON only.",
                      "schema": obj({"status": {"type": "string", "enum": ["normal", "anomaly"]},
                                     "fault_type": {"type": "string", "enum": ["normal_hum", "leak", "bearing_impulsive", "electrical", "other"]},
                                     "peak_khz": {"type": "number"}, "confidence": {"type": "number"}}),
                      "truth": {"leak": status == "leak", "leak_type": leak, "noise": noise, "file": fn,
                                "peak_khz": float(f[keep][f[keep] >= 100][np.argmax(psd[f[keep] >= 100])] / 1000),
                                "hf_frac": float(psd[f[keep] >= 15000].sum() / psd.sum())}})
    write("acoustic", items)


class HTTPRangeFile(io.RawIOBase):
    """A seekable file over HTTP Range requests, so zipfile can pull single members out of a 3 GB archive."""
    def __init__(self, url):
        self.pos = 0
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
            self.size, self.url = int(r.headers["Content-Length"]), r.geturl()
    def seekable(self): return True
    def readable(self): return True
    def tell(self): return self.pos
    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos
    def read(self, n=-1):
        n = self.size - self.pos if n < 0 else n
        if n <= 0 or self.pos >= self.size: return b""
        end = min(self.size, self.pos + n) - 1
        req = urllib.request.Request(self.url, headers={"Range": "bytes=%d-%d" % (self.pos, end)})
        with urllib.request.urlopen(req, timeout=120) as r: data = r.read()
        self.pos += len(data)
        return data
    def readinto(self, b):
        data = self.read(len(b)); b[:len(data)] = data
        return len(data)


def gauge():   # RPM-10K test: real substation dials (biao1-6), read the needle
    os.makedirs(R + "/gauge/img", exist_ok=True)
    def numeric(v):
        try:
            float(v); return True
        except (TypeError, ValueError):
            return False   # some labels are notes ("Too vague. Unable to read.")
    labels = [r for r in json.load(open(D + "/rpm/test.json")) if r["meter_type"].startswith("biao") and numeric(r["reading"])]
    picked = balanced(labels, lambda r: r["meter_type"], N)
    z = zipfile.ZipFile(HTTPRangeFile("https://hf-mirror.com/datasets/BommadiUday/Gauge-Meter/resolve/main/RPM-10K.zip"))
    items = []
    for i, r in enumerate(picked):
        i = key(r["image"])
        p = "%s/gauge/img/%s.jpg" % (R, i)
        save(Image.open(io.BytesIO(z.read("RPM-10K/images/" + r["image"]))), p)
        items.append({"id": "rpm-%s" % i, "image": p, "max_tokens": 96,
                      "prompt": "Read the main needle of this instrument. Ignore digital counters. Reply with JSON only.",
                      "schema": obj({"value": {"type": "number"}, "unit": {"type": "string"}, "scale_min": {"type": "number"},
                                     "scale_max": {"type": "number"}, "confidence": {"type": "number"}}),
                      "truth": {"reading": float(r["reading"]), "range": float(r["range"]), "meter_type": r["meter_type"],
                                "conditions": r["environment_conditions"], "image": r["image"]}})
    write("gauge", items)


def thermal():   # herislab electric_motor: normal vs overheating (no colour scale, like Orbit's thermal panes)
    os.makedirs(R + "/thermal/img", exist_ok=True)
    lf = D + "/herislab/labels2.json"
    labels = json.load(open(lf if os.path.exists(lf) else D + "/herislab/labels.json"))
    items = []
    for i, r in enumerate(balanced(labels, lambda r: r["label"], N)):
        i = key(r["file"])
        p = "%s/thermal/img/%s.jpg" % (R, i)
        save(Image.open(D + "/herislab/img/" + r["file"]), p)
        items.append({"id": "motor-%s" % i, "image": p, "max_tokens": 64,
                      "prompt": "Thermal image of an electric motor; there is no colour scale. Is the motor overheating? "
                                "Reply with JSON only.",
                      "schema": obj({"status": {"type": "string", "enum": ["normal", "overheating"]},
                                     "hot_component": {"type": "string"}, "confidence": {"type": "number"}}),
                      "truth": {"overheating": r["label"] == "fault", "file": r["file"]}})
    write("thermal", items)


for name, fn in (("visual", visual), ("navigation", navigation), ("thermal", thermal), ("acoustic", acoustic), ("gauge", gauge)):
    if not ONLY or name in ONLY:
        try:
            fn()
        except Exception as exc:
            print(name, "FAILED", type(exc).__name__, str(exc)[:300], flush=True)
