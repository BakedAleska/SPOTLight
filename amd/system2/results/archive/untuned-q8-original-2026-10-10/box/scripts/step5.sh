source /root/env.sh
python3 -m venv /root/s2env && /root/s2env/bin/pip install -q -r /root/SpotTheDOT/amd/system2/requirements.txt
echo "PIP_EXIT=$?"
cd /root/SpotTheDOT/amd/system2 && /root/s2env/bin/python scripts/report.py --runner both --model-url http://127.0.0.1:8080 \
  --model qwen3.8-27b-q8_0 --label amd-gfx1100-rocm-qwen38-27b-q8 --out-dir results/report/amd-qwen38-27b-q8
echo "REPORT_EXIT=$?"
/root/s2env/bin/python scripts/report.py --rebuild results/report/amd-qwen38-27b-q8 --out-dir /root/tmp/rb
echo "REBUILD_EXIT=$?"
