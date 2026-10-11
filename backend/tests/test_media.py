"""GET /assets/media (the Assets page gallery list) and GET /assets/media/{id} (the stored bytes)."""
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


async def test_media_list_has_metadata_but_no_bytes(h):
    h.repo.media.append({"media_id": "demo:thermal:motor-301-1", "action_id": None, "mission_id": None,
                         "media_type": "image/jpeg", "bytes": len(JPEG), "captured_at": "2026-09-01T09:00:00",
                         "metadata": {"inspection_type": "thermal", "asset_id": "MOTOR-301", "mock": True}, "content": JPEG})
    r = await h.client.get("/api/v1/assets/media")
    assert r.status_code == 200
    [row] = r.json()
    assert row["media_id"] == "demo:thermal:motor-301-1" and row["metadata"]["asset_id"] == "MOTOR-301"
    assert "content" not in row           # and it reached list_media, not the /assets/{asset_id} route


async def test_media_bytes_are_served_with_their_type_and_cached(h):
    h.repo.media.append({"media_id": "demo:gauge:pg-301", "media_type": "image/jpeg", "content": JPEG, "metadata": {}})
    r = await h.client.get("/api/v1/assets/media/demo:gauge:pg-301")
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg" and "max-age" in r.headers["cache-control"]
    assert (await h.client.get("/api/v1/assets/media/demo:none")).status_code == 404

