from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from PIL import Image

_FRAME_ID_RE = re.compile(r"^frame_[0-9a-f]{32}$")


def default_frame_cache_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "game-input-mcp"
    return base / "frames"


@dataclass(frozen=True)
class FrameRecord:
    frame_id: str
    image_path: Path
    metadata_path: Path
    metadata: dict[str, Any]
    created_at: float
    thumb_path: Path | None = None


class FrameCache:
    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        ttl_sec: int = 30,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.directory = Path(directory) if directory is not None else default_frame_cache_dir()
        self.ttl_sec = ttl_sec
        self._now = now
        self.directory.mkdir(parents=True, exist_ok=True)

    def _thumb_path(self, frame_id: str) -> Path:
        # `thumb_<id>.png`, not `<frame_id>.thumb.png`: cleanup() treats any
        # frame_*.png without a matching .json as an orphan and would delete it.
        return self.directory / f"thumb_{frame_id}.png"

    def store(
        self,
        image: Image.Image,
        metadata: dict[str, Any],
        thumb: Image.Image | None = None,
    ) -> FrameRecord:
        frame_id = f"frame_{uuid4().hex}"
        created_at = self._now()
        enriched = {
            **metadata,
            "frame_id": frame_id,
            "created_at": created_at,
        }
        image_path = self.directory / f"{frame_id}.png"
        metadata_path = self.directory / f"{frame_id}.json"
        image.save(image_path, format="PNG", optimize=True)
        thumb_path = None
        if thumb is not None:
            thumb_path = self._thumb_path(frame_id)
            thumb.save(thumb_path, format="PNG", optimize=True)
        metadata_path.write_text(json.dumps(enriched, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        return FrameRecord(frame_id, image_path, metadata_path, enriched, created_at, thumb_path)

    def get(self, frame_id: str) -> FrameRecord | None:
        if _FRAME_ID_RE.fullmatch(frame_id) is None:
            return None
        image_path = self.directory / f"{frame_id}.png"
        metadata_path = self.directory / f"{frame_id}.json"
        if not image_path.exists() or not metadata_path.exists():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                return None
            created_at = float(metadata.get("created_at", 0.0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if self._now() - created_at > self.ttl_sec:
            return None
        thumb_path = self._thumb_path(frame_id)
        return FrameRecord(
            frame_id, image_path, metadata_path, metadata, created_at, thumb_path if thumb_path.exists() else None
        )

    def cleanup(self) -> int:
        removed = 0
        for metadata_path in self.directory.glob("frame_*.json"):
            frame_id = metadata_path.stem
            record = self.get(frame_id)
            if record is not None:
                continue
            image_path = self.directory / f"{frame_id}.png"
            for path in (metadata_path, image_path, self._thumb_path(frame_id)):
                if path.exists():
                    path.unlink()
                    removed += 1
        for image_path in self.directory.glob("frame_*.png"):
            frame_id = image_path.stem
            metadata_path = self.directory / f"{frame_id}.json"
            if not metadata_path.exists() and image_path.exists():
                image_path.unlink()
                removed += 1
        for thumb_path in self.directory.glob("thumb_frame_*.png"):
            frame_id = thumb_path.stem[len("thumb_"):]
            if not (self.directory / f"{frame_id}.json").exists():
                thumb_path.unlink()
                removed += 1
        return removed
