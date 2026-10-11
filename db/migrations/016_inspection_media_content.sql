-- target: logs
-- Image bytes for inspection media served by GET /assets/media/{media_id} (the Assets page gallery). Additive and
-- idempotent. Kit samples keep content NULL (their uri points at the repo); demo media loaded by
-- scripts/load_inspection_media.py carry their JPEG here, so Vercel needs no object storage.
ALTER TABLE raw.inspection_media ADD COLUMN IF NOT EXISTS content bytea;
