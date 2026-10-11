"""Regression smoke over HTTP: kit photos through the vision service, then System 2 /v1/assess, plus two fixtures.

Each photo case builds its System 2 request exactly as the orchestrator does with DEMO_IMAGES=1 (demo_image,
vision_rows, ocr_lines, system2_image from amd/orchestrator/orchestrator/assess.py; pairing "demo", no `logged`).
Every artifact goes under --out: per call the request body, the vision response (annotated image as a .jpg),
System 2's headers, response and wall time. Run System 2 with SYSTEM2_CACHE_DIR=off and SYSTEM2_RECORD_DIR=<out>
so each call is a genuine generation and the raw attempts land next to these files; this script checks the
X-System2-Cache header and records it, it does not assume.

    VISION_URL=http://127.0.0.1:8084 VISION_API_KEY=... SYSTEM2_URL=http://127.0.0.1:8083 SYSTEM2_API_KEY=... \
      python scripts/photo_smoke.py --out /root/runs/<run-id> --runs 2
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'amd/orchestrator'))
from orchestrator.assess import demo_image, ocr_lines, reasons, system2_image, vision_rows  # noqa: E402

KIT = ROOT / 'web/public/kit/inspection'
S2 = ROOT / 'amd/system2'
TRUST_MIN, OCR_TOP_N = 0.5, 8   # orchestrator defaults (VISION_TRUST_MIN, OCR_TOP_N)
# case -> (request source, action_type for the demo photo or None for a fixture sent as-is)
CASES = {
    'gauge_photo': ('fixtures/backend/data_quality.json', 'GAUGE_READING'),
    'thermal_photo': ('data/heldout_packets.json#heldout-A0118', 'THERMAL_INSPECTION'),   # smoke input only
    'gauge_disagree': ('fixtures/multimodal/gauge_disagree.json', None),
    'text_equipment': ('fixtures/backend/equipment.json', None),
}


def post(url, body, key, timeout=90):
    req = urllib.request.Request(url, json.dumps(body).encode(), {'Content-Type': 'application/json',
                                                                    'X-API-Key': key})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
        return data, {k.lower(): v for k, v in r.headers.items()}, int((time.perf_counter() - t0) * 1000)


def load_request(source):
    path, _, case_id = source.partition('#')
    data = json.loads((S2 / path).read_text(encoding='utf-8'))
    if case_id:
        return copy.deepcopy(next(c for c in data['cases'] if c['case_id'] == case_id)['request'])
    return copy.deepcopy(data['request'])


def photo_body(req, action_type, request_id, out, env):
    image, kind, media_id = demo_image(action_type, str(KIT))
    vbody = {'request_id': request_id, 'media_id': media_id, 'kind': kind, 'action_type': action_type,
             'image_b64': image, 'annotate': True}
    vresp, _, vms = post(env['VISION_URL'].rstrip('/') + '/v1/vision', vbody, env['VISION_API_KEY'])
    annotated = vresp.get('annotated_image_b64')
    if annotated:
        (out / (request_id + '.annotated.jpg')).write_bytes(base64.b64decode(annotated))
    saved = dict(vresp, annotated_image_b64='<%s.annotated.jpg>' % request_id if annotated else None)
    (out / (request_id + '.vision.json')).write_text(json.dumps(saved, indent=1, ensure_ascii=False), 'utf-8')
    rows = vision_rows(vresp, 'demo', TRUST_MIN, None)
    ocr = ocr_lines(vresp, OCR_TOP_N)
    s2_img, note = system2_image(annotated)
    ctx = copy.deepcopy(req['context'])
    ctx.pop('vision', None)
    ctx.pop('ocr', None)
    if rows:
        ctx['vision'] = rows
    if ocr:
        ctx['ocr'] = ocr
    flag = req.get('flag') or {'id': 'flag:%s' % req['flag_id']}
    body = {'flag_id': req['flag_id'], 'request_id': request_id, 'flag': flag, 'event': None,
            'reasons': [r.get('detail') if isinstance(r.get('detail'), str) else None for r in reasons(flag)],
            'context': ctx}
    if s2_img:
        body['image_b64'] = s2_img
    return body, {'photo': media_id + '.jpg', 'vision_ms': vms, 'vision_kind': vresp.get('kind'),
                  'router': vresp.get('router'), 'rows': rows, 'ocr': [o['text'] for o in ocr],
                  'alert': vresp.get('alert'), 'image_note': note}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--runs', type=int, default=2)
    ap.add_argument('--cases', nargs='+', default=list(CASES), choices=list(CASES))
    args = ap.parse_args(argv)
    env = {k: os.environ[k] for k in ('VISION_URL', 'VISION_API_KEY', 'SYSTEM2_URL', 'SYSTEM2_API_KEY')}
    args.out.mkdir(parents=True, exist_ok=True)
    results = args.out / 'results.jsonl'
    for case in args.cases:
        source, action_type = CASES[case]
        for run in range(1, args.runs + 1):
            rid = '%s-run%d' % (case, run)
            req = load_request(source)
            if action_type:
                body, vision = photo_body(req, action_type, rid, args.out, env)
            else:
                body, vision = dict(req, request_id=rid), None
            (args.out / (rid + '.request.json')).write_text(json.dumps(body, ensure_ascii=False), 'utf-8')
            resp, hdr, ms = post(env['SYSTEM2_URL'].rstrip('/') + '/v1/assess', body, env['SYSTEM2_API_KEY'])
            line = {'request_id': rid, 'case': case, 'source': source, 'run': run, 'wall_ms': ms,
                    'cache': hdr.get('x-system2-cache'), 'image_status': hdr.get('x-system2-image'),
                    'path': resp.get('path'), 'model': resp.get('model'), 'latency_ms': resp.get('latency_ms'),
                    'recommended_action': resp.get('recommended_action'),
                    'cited': [e['ids'] for e in resp.get('evidence', [])], 'answer': resp.get('answer'),
                    'response': resp, 'vision': vision}
            with open(results, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + '\n')
            print('%-16s run %d | %6d ms | cache %-4s | image %-9s | path %-8s | %s | cited %s' % (
                case, run, ms, line['cache'], line['image_status'], line['path'], line['recommended_action'],
                line['cited']))
            print('    answer (%d chars): %s' % (len(resp.get('answer') or ''), resp.get('answer')))


if __name__ == '__main__':
    main()
