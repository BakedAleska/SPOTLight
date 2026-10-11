# Live runner (scripts/live_runner.py): claims live runs queued from the web console and walks them, calling the AMD
# box services through SSH tunnels on the host (host.docker.internal) and the backend's admin API. Standard library
# only; it never holds database credentials. Build context is the REPO ROOT (it ships the stop photos):
#   docker build -f scripts/live_runner.Dockerfile -t spot-live-runner .
# Configured only by env: BACKEND_URL, ADMIN_TOKEN, SYSTEM1_URL, EMBED_URL, SYSTEM2_URL, VISION_URL,
# SYSTEM2_API_KEY, VISION_API_KEY, LIVE_TRAVEL_S, LIVE_WORK_S, SYSTEM2_TIMEOUT_S, SYSTEM2_STREAM (compose sets it
# from LIVE_SYSTEM2_STREAM), LIVE_DRAFT_INTERVAL_S, RUNNER_NAME. Runbook: docs/LIVE-RUNBOOK.md, docs/STREAMING.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LIVE_HEARTBEAT_FILE=/tmp/live-runner.heartbeat
WORKDIR /app
COPY scripts/live_runner.py scripts/live_runner.py
COPY web/public/kit/inspection/ web/public/kit/inspection/
COPY amd/system2/fixtures/multimodal/gauge_demo.json amd/system2/fixtures/multimodal/gauge_demo.json

RUN useradd --uid 10003 --create-home runner
USER runner

# Healthy while the poll loop or a walk touched the heartbeat in the last 60 s.
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,time; f=os.environ['LIVE_HEARTBEAT_FILE']; sys.exit(0 if os.path.exists(f) and time.time()-os.path.getmtime(f) < 60 else 1)"

ENTRYPOINT ["python", "scripts/live_runner.py"]
