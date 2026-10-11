# syntax=docker/dockerfile:1
# One-shot db job: migrate / seed / ingest / graph / status / smoke against DATABASE_URL (Neon or local).
# CPU-only and torch-free: ingestion calls the embedding service over HTTP (EMBED_URL).
# Build context is the REPO ROOT (db/Dockerfile.dockerignore limits what is sent):
#
#   docker build -f db/Dockerfile -t spot-db .
#   docker run --rm -e DATABASE_URL=... -e EMBED_URL=http://host.docker.internal:8081 spot-db all
#   docker run --rm -e DATABASE_URL=... spot-db migrate      # or: seed | refresh | ingest | graph | status | smoke | reset --yes
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EMBED_URL=http://embed:8081 \
    EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B \
    EMBED_DIM=1024

WORKDIR /app
COPY db/requirements.txt db/requirements.txt
RUN pip install -r db/requirements.txt

COPY db/ db/
COPY amd/embed/client.py amd/embed/ingest.py amd/embed/
COPY amd/system1/triage/ amd/system1/triage/
COPY amd/system1/data/kit/ amd/system1/data/kit/
COPY ["SPOTDATA/SPOT_KnightHacks_Challenge_Kit Part Two/", "SPOTDATA/SPOT_KnightHacks_Challenge_Kit Part Two/"]
COPY ["SPOTDATA/SPOT_KnightHacks_Challenge_Kit Part One/04_ASSETS/sample_inspection_images/", "SPOTDATA/SPOT_KnightHacks_Challenge_Kit Part One/04_ASSETS/sample_inspection_images/"]

RUN useradd --create-home --uid 10001 app && chmod +x db/job.sh
USER app
# One-shot job (compose: restart "no"); it exits when done, so there is nothing to probe. Explicit per CLAUDE.md.
HEALTHCHECK NONE
ENTRYPOINT ["db/job.sh"]
CMD ["all"]
