"""Vercel build step for the backend (vercel.json `buildCommand`; README "Deploy on Vercel").

The Vercel project's Root Directory is `backend/`, and the Python runtime bundles only files under it. The backend
imports the shared, torch-free triage modules and reads the kit data, both outside `backend/`, so this copies them
into `backend/_shared/`, mirroring the repo layout (config.py falls back to it when the repo copy is absent):

    amd/system1/triage/{__init__,events,kit,policy,questions,render}.py -> _shared/amd/system1/triage/
    amd/system1/data/kit/*                                                -> _shared/amd/system1/data/kit/

Nothing is committed: `_shared/` is git-ignored and rebuilt on every deploy from the canonical files, so the policy
drift guard (tests/test_policy_consistency.py) still checks the code that ships. Needs the project setting "Include
source files outside of the Root Directory in the Build Step" (on by default). Standard library only; fails loudly if a
source file is missing, so a broken bundle never deploys.

    python3 vercel_build.py [--check]     # --check: verify the sources exist, copy nothing
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
REPO = BACKEND.parent
OUT = BACKEND / "_shared"

# Only the modules the backend imports (app/services/triage.py). harness.py, trainer.py, ... import torch: never copied.
TRIAGE_MODULES = ("__init__.py", "events.py", "kit.py", "policy.py", "questions.py", "render.py")
TRIAGE_SRC = Path("amd") / "system1" / "triage"
KIT_SRC = Path("amd") / "system1" / "data" / "kit"
KIT_SUFFIXES = (".csv", ".ndjson", ".json")


def plan() -> list:
    """(source, destination) pairs, relative to the repo root and to OUT."""
    pairs = [(TRIAGE_SRC / m, TRIAGE_SRC / m) for m in TRIAGE_MODULES]
    kit = REPO / KIT_SRC
    if kit.is_dir():
        pairs += [(KIT_SRC / f.name, KIT_SRC / f.name) for f in sorted(kit.iterdir())
                  if f.is_file() and f.suffix in KIT_SUFFIXES]
    return pairs


def main(argv: list) -> int:
    pairs = plan()
    missing = [str(src) for src, _ in pairs if not (REPO / src).is_file()]
    if not any(src.parent == KIT_SRC for src, _ in pairs) or not (REPO / KIT_SRC / "replay.ndjson").is_file():
        missing.append(str(KIT_SRC / "replay.ndjson"))
    if missing:
        print("vercel_build: missing %s under %s. Is 'Include source files outside of the Root Directory in the "
              "Build Step' enabled for this Vercel project?" % (", ".join(missing), REPO), file=sys.stderr)
        return 1
    if "--check" in argv:
        print("vercel_build: %d files ready to copy" % len(pairs))
        return 0
    shutil.rmtree(OUT, ignore_errors=True)
    for src, dst in pairs:
        (OUT / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / src, OUT / dst)
    print("vercel_build: copied %d files into %s" % (len(pairs), OUT.relative_to(REPO)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
