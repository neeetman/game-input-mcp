from __future__ import annotations

import json

from PIL import Image

from windows_input_mcp.frames import FrameCache


def test_store_writes_png_and_json_metadata(tmp_path) -> None:
    cache = FrameCache(tmp_path, ttl_sec=60)
    image = Image.new("RGB", (4, 3), "red")
    metadata = {"target": {"pid": 1}, "image": {"width": 4, "height": 3}}

    record = cache.store(image, metadata)

    assert record.frame_id.startswith("frame_")
    assert record.image_path.exists()
    assert record.metadata_path.exists()
    assert json.loads(record.metadata_path.read_text(encoding="utf-8"))["target"]["pid"] == 1
    assert cache.get(record.frame_id).image_path == record.image_path


def test_get_returns_none_for_missing_frame(tmp_path) -> None:
    cache = FrameCache(tmp_path, ttl_sec=60)

    assert cache.get("frame_missing") is None


def test_cleanup_removes_expired_files(tmp_path) -> None:
    now = [1000.0]
    cache = FrameCache(tmp_path, ttl_sec=10, now=lambda: now[0])
    record = cache.store(Image.new("RGB", (1, 1), "blue"), {"value": 1})
    now[0] = 1011.0

    removed = cache.cleanup()

    assert removed == 2
    assert not record.image_path.exists()
    assert not record.metadata_path.exists()
