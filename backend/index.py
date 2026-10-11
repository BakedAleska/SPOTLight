"""Vercel entrypoint (README "Deploy on Vercel"). Vercel's Python runtime looks for a top-level `app` in `index.py`
at the project's Root Directory (`backend/`) before `app/main.py`; importing it here keeps `app` a package, so its
relative imports work exactly as under uvicorn (`uvicorn app.main:app`). Docker and local runs never load this file.
"""
from app.main import app  # noqa: F401
