"""Vercel entrypoint (project Root Directory `amd/mcp`): the HTTP app `python -m spot_mcp --http` serves.

Vercel's Python runtime finds this file by name, serves its top-level `app` and runs the ASGI lifespan, which starts
the MCP session manager. Config is env vars only (README "Deploy on Vercel"). The URL is public, so MCP_AUTH_TOKEN
is required here; the database connection opens lazily on the first tool call.
"""
import logging
import os

from spot_mcp.server import create_app

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")

_token = os.environ.get("MCP_AUTH_TOKEN")
if not _token:
    raise RuntimeError("MCP_AUTH_TOKEN is not set: refusing to serve the database on a public URL")

app = create_app(host="0.0.0.0", token=_token)
