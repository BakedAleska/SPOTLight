"""Load demo inspection media into the database for the Assets page gallery (GET /assets/media).

    DATABASE_URL=<owner url> python scripts/load_inspection_media.py <dir with manifest.json and img/>

The manifest (built outside the repo; the images are not committed) lists mock `assets` and their `media`:
{"assets": [{asset_id, asset_name, asset_type, location_id, criticality}],
 "media":  [{media_id, asset_id, inspection_type, file, captured_at, condition, finding, reading?, source, sha256}]}

Assets are upserted into `assets` (their ids never clash with the kit's). Each image goes into
raw.inspection_media with its bytes in `content` (migration 016) and the mock details in `metadata`, always marked
`mock: true` with the image's source and licence. raw.* is append-only, so an existing media_id is left as it is.
Needs DATABASE_URL (assets) and LOGS_DATABASE_URL if raw.* lives elsewhere (defaults to DATABASE_URL).
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb


def main(folder: Path) -> None:
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    main_url = os.environ["DATABASE_URL"]
    logs_url = os.environ.get("LOGS_DATABASE_URL") or main_url
    names = {}
    with psycopg.connect(main_url) as conn, conn.transaction():
        for a in manifest["assets"]:
            conn.execute(
                "INSERT INTO assets (asset_id, asset_name, asset_type, location_id, criticality) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (asset_id) DO UPDATE SET asset_name = EXCLUDED.asset_name, asset_type = EXCLUDED.asset_type, "
                "location_id = EXCLUDED.location_id, criticality = EXCLUDED.criticality",
                (a["asset_id"], a["asset_name"], a["asset_type"], a["location_id"], a["criticality"]))
        for row in conn.execute("SELECT s.asset_id, s.asset_name, s.location_id, l.location_name FROM assets s "
                                "LEFT JOIN locations l USING (location_id)"):
            names[row[0]] = {"asset_name": row[1], "location_id": row[2], "location_name": row[3]}
    inserted = 0
    with psycopg.connect(logs_url) as conn, conn.transaction():
        for m in manifest["media"]:
            data = (folder / m["file"]).read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if m.get("sha256") and m["sha256"] != sha:
                raise SystemExit("sha256 mismatch for %s" % m["file"])
            meta = {"mock": True, "inspection_type": m["inspection_type"], "asset_id": m["asset_id"],
                    **names.get(m["asset_id"], {}), "condition": m.get("condition"), "finding": m.get("finding"),
                    "reading": m.get("reading"), "source": m.get("source"),
                    "note": "demo imagery with mock asset details; not a SPOT capture"}
            cur = conn.execute(
                "INSERT INTO raw.inspection_media (media_id, action_id, mission_id, media_type, uri, sha256, bytes, "
                "captured_at, metadata, content) VALUES (%s, NULL, NULL, 'image/jpeg', %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (media_id) DO NOTHING",
                (m["media_id"], "db:raw.inspection_media/" + m["media_id"], sha, len(data), m["captured_at"],
                 Jsonb(meta), data))
            inserted += cur.rowcount
    print("assets upserted: %d | media inserted: %d of %d (existing ids left unchanged)"
          % (len(manifest["assets"]), inserted, len(manifest["media"])))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
