source /root/env.sh
cd /root/SpotTheDOT/amd/system2 && export SYSTEM2_API_KEY="dev-$(openssl rand -hex 8)"
tmux new -d -s s2 "source /root/env.sh; SYSTEM2_API_KEY=$SYSTEM2_API_KEY SYSTEM2_CACHE_DIR=/root/s2cache SYSTEM2_LLAMA_URL=http://127.0.0.1:8080 SYSTEM2_MODEL=qwen3.8-27b-q8_0 SYSTEM2_VISION_MODEL=qwen3.8-27b-q8_0 /root/s2env/bin/python scripts/serve.py 2>&1 | tee /root/system2.log"
for i in $(seq 1 120); do curl -s 127.0.0.1:8083/health | grep -q live && break; sleep 1; done; curl -s 127.0.0.1:8083/health; echo
for f in fixtures/backend/equipment.json fixtures/multimodal/gauge_disagree.json; do
  /root/s2env/bin/python -c "import json,sys;print(json.dumps(json.load(open(sys.argv[1]))['request']))" $f > /root/tmp/req.json
  for i in 1 2; do curl -s -D /root/tmp/h -o /root/tmp/r.json -H "X-API-Key: $SYSTEM2_API_KEY" -H "Content-Type: application/json" \
    --data @/root/tmp/req.json 127.0.0.1:8083/v1/assess; echo "$f call $i:"; grep -i -E "^HTTP|x-system2-(cache|image)" /root/tmp/h
    /root/s2env/bin/python -c "import json;r=json.load(open('/root/tmp/r.json'));print({k:r.get(k) for k in ('path','model','latency_ms','recommended_action')}, [e['ids'] for e in r.get('evidence',[])])"; done; done
/root/s2env/bin/python scripts/precompute.py --model-url http://127.0.0.1:8080 --model qwen3.8-27b-q8_0 --cache-dir /root/s2cache-fresh \
  --requests data/dev_packets.json fixtures/backend/*.json | tail -2
echo "STEP6_DONE"
