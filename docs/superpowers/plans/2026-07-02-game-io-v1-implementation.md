# Game I/O V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows-only, non-injected game screenshot and input MCP v1 with target-aware capture, file-backed frame metadata, capture-to-click coordinate mapping, and scan-code key controls.

**Architecture:** Keep the existing MCP server plus elevated per-user daemon split. Add focused modules for target resolution, coordinate geometry, frame caching, capture backends, and key mapping, then wire those modules into daemon handlers and MCP tools while preserving the current pid-based tools.

**Tech Stack:** Python 3.10+, `mcp`, `pywin32`, `Pillow`, `mss`, optional `dxcam`, pytest for tests, Win32 `SendInput`, per-user named-pipe IPC.

## Global Constraints

- Version 1 stays Windows-only and non-injected.
- The package name remains `windows_input_mcp` and existing console scripts remain available.
- Start v1 capture with `dxcam`, `mss`, and `pillow`; defer `windows_graphics_capture`.
- Use a file-backed per-user frame cache for daemon-to-server image transfer.
- Keep the MCP surface focused on game capture and input; do not add filesystem, registry, shell, browser, DOM, or broad desktop automation tools.
- Preserve the elevated daemon model and user-scoped named pipe DACL.
- Preserve backward compatibility for existing pid-based tools during migration.
- Treat `framebuffer` as a deprecated alias for `capture`.

---

## File Structure

- Modify `pyproject.toml`: add capture dependencies and dev test dependency group.
- Create `tests/`: unit tests for configuration, models, targets, geometry, frame cache, capture backends, daemon handlers, server wrappers, and key mapping.
- Create `windows_input_mcp/models.py`: shared dataclasses and structured error helpers.
- Create `windows_input_mcp/targets.py`: target parsing, target resolution, and target listing around existing Win32 window data.
- Create `windows_input_mcp/geometry.py`: coordinate transforms for `screen`, `client`, `capture`, `normalized`, and `framebuffer`.
- Create `windows_input_mcp/frames.py`: per-user PNG and JSON metadata cache keyed by `frame_id`.
- Create `windows_input_mcp/capture/__init__.py`: capture package exports.
- Create `windows_input_mcp/capture/base.py`: backend protocol, registry, auto fallback.
- Create `windows_input_mcp/capture/pillow_backend.py`: Pillow capture backend.
- Create `windows_input_mcp/capture/mss_backend.py`: mss capture backend.
- Create `windows_input_mcp/capture/dxcam_backend.py`: dxcam capture backend.
- Create `windows_input_mcp/capture/service.py`: target-aware capture orchestration and frame cache storage.
- Create `windows_input_mcp/input/__init__.py`: input package exports.
- Create `windows_input_mcp/input/keys.py`: key name to scan-code and virtual-key mapping.
- Modify `windows_input_mcp/win32.py`: expose hwnd-based window info, window listing, detailed input counts, and key down/up/tap primitives.
- Modify `windows_input_mcp/daemon.py`: register target, capture, frame-aware mouse, and key handlers.
- Modify `windows_input_mcp/server.py`: expose new MCP tools and keep compatibility wrappers.
- Modify `README.md`: update project positioning, install dependencies, tools, and smoke-test guidance.

---

### Task 1: Dependency And Test Baseline

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/test_project_baseline.py`

**Interfaces:**
- Consumes: existing `pyproject.toml`
- Produces: declared runtime dependencies `pillow`, `mss`, `dxcam`; declared dev dependency `pytest`

- [ ] **Step 1: Write the failing pyproject test**

Create `tests/test_project_baseline.py`:

```python
from __future__ import annotations

import tomllib
from pathlib import Path


def _pyproject() -> dict:
    return tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))


def _dependency_names(dependencies: list[str]) -> set[str]:
    names: set[str] = set()
    for dep in dependencies:
        marker_free = dep.split(";", 1)[0].strip()
        name = marker_free.split(">=", 1)[0].split("==", 1)[0].strip().lower()
        names.add(name)
    return names


def test_capture_runtime_dependencies_are_declared() -> None:
    project = _pyproject()["project"]
    names = _dependency_names(project["dependencies"])

    assert "pillow" in names
    assert "mss" in names
    assert "dxcam" in names


def test_pytest_dev_dependency_is_declared() -> None:
    project = _pyproject()["project"]
    dev = project["optional-dependencies"]["dev"]
    names = _dependency_names(dev)

    assert "pytest" in names
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```powershell
uv run --with pytest pytest tests/test_project_baseline.py -q
```

Expected: FAIL because `pillow`, `mss`, `dxcam`, and `project.optional-dependencies.dev` are not declared.

- [ ] **Step 3: Update `pyproject.toml`**

Change the dependencies section to include capture dependencies and add dev extras:

```toml
dependencies = [
    "mcp>=1.0.0",
    "pillow>=10.0.0",
    "mss>=9.0.0; sys_platform == 'win32'",
    "dxcam>=0.3.0; sys_platform == 'win32'",
    "pywin32>=306; sys_platform == 'win32'",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```powershell
uv run --extra dev pytest tests/test_project_baseline.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml tests/test_project_baseline.py
git commit -m "test: add project dependency baseline"
```

---

### Task 2: Shared Models And Structured Errors

**Files:**
- Create: `windows_input_mcp/models.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Consumes: no new project interfaces
- Produces:
  - `Rect(left: int, top: int, right: int, bottom: int)`
  - `TargetSpec(pid: int | None = None, hwnd: int | None = None)`
  - `TargetInfo(...)`
  - `ok_response(**fields) -> dict`
  - `error_response(error_code: str, message: str, retryable: bool = False, **details) -> dict`

- [ ] **Step 1: Write failing model tests**

Create `tests/test_models.py`:

```python
from __future__ import annotations

import pytest

from windows_input_mcp.models import Rect, TargetSpec, error_response, ok_response


def test_rect_width_height_and_list() -> None:
    rect = Rect(left=-10, top=20, right=90, bottom=70)

    assert rect.width == 100
    assert rect.height == 50
    assert rect.to_list() == [-10, 20, 90, 70]


def test_target_spec_accepts_legacy_pid() -> None:
    spec = TargetSpec.from_value(1234)

    assert spec.pid == 1234
    assert spec.hwnd is None


def test_target_spec_accepts_target_dict() -> None:
    spec = TargetSpec.from_value({"pid": 1234, "hwnd": 5678})

    assert spec.pid == 1234
    assert spec.hwnd == 5678


def test_target_spec_rejects_empty_target() -> None:
    with pytest.raises(ValueError, match="pid or hwnd"):
        TargetSpec.from_value({})


def test_response_helpers_have_stable_shape() -> None:
    assert ok_response(pid=1) == {"success": True, "pid": 1}
    assert error_response(
        "TARGET_NOT_FOUND",
        "No target",
        retryable=True,
        pid=1,
    ) == {
        "success": False,
        "error_code": "TARGET_NOT_FOUND",
        "message": "No target",
        "retryable": True,
        "details": {"pid": 1},
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run --extra dev pytest tests/test_models.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'windows_input_mcp.models'`.

- [ ] **Step 3: Create `windows_input_mcp/models.py`**

Add:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def to_list(self) -> list[int]:
        return [self.left, self.top, self.right, self.bottom]

    @classmethod
    def from_list(cls, value: list[int] | tuple[int, int, int, int]) -> "Rect":
        if len(value) != 4:
            raise ValueError("rect must contain exactly four integers")
        return cls(int(value[0]), int(value[1]), int(value[2]), int(value[3]))


@dataclass(frozen=True)
class TargetSpec:
    pid: int | None = None
    hwnd: int | None = None

    @classmethod
    def from_value(cls, value: int | dict[str, Any] | "TargetSpec") -> "TargetSpec":
        if isinstance(value, TargetSpec):
            spec = value
        elif isinstance(value, int):
            spec = cls(pid=value)
        elif isinstance(value, dict):
            pid = value.get("pid")
            hwnd = value.get("hwnd")
            spec = cls(
                pid=int(pid) if pid is not None else None,
                hwnd=int(hwnd) if hwnd is not None else None,
            )
        else:
            raise ValueError("target must be a pid integer or object with pid or hwnd")
        if spec.pid is None and spec.hwnd is None:
            raise ValueError("target must include pid or hwnd")
        return spec

    def to_dict(self) -> dict[str, int]:
        out: dict[str, int] = {}
        if self.pid is not None:
            out["pid"] = self.pid
        if self.hwnd is not None:
            out["hwnd"] = self.hwnd
        return out


@dataclass(frozen=True)
class TargetInfo:
    hwnd: int
    pid: int
    title: str
    window_rect: Rect
    client_rect_screen: Rect
    client_size: tuple[int, int]
    client_screen_origin: tuple[int, int]
    dpi: int
    is_foreground: bool
    is_minimized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "pid": self.pid,
            "title": self.title,
            "window_rect": self.window_rect.to_list(),
            "client_rect_screen": self.client_rect_screen.to_list(),
            "client_size": list(self.client_size),
            "client_screen_origin": list(self.client_screen_origin),
            "dpi": self.dpi,
            "is_foreground": self.is_foreground,
            "is_minimized": self.is_minimized,
        }


def ok_response(**fields: Any) -> dict[str, Any]:
    return {"success": True, **fields}


def error_response(
    error_code: str,
    message: str,
    *,
    retryable: bool = False,
    **details: Any,
) -> dict[str, Any]:
    return {
        "success": False,
        "error_code": error_code,
        "message": message,
        "retryable": retryable,
        "details": details,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```powershell
uv run --extra dev pytest tests/test_models.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add windows_input_mcp/models.py tests/test_models.py
git commit -m "feat: add shared game io models"
```

---

### Task 3: Target Resolution And Window Listing

**Files:**
- Modify: `windows_input_mcp/win32.py`
- Create: `windows_input_mcp/targets.py`
- Create: `tests/test_targets.py`

**Interfaces:**
- Consumes: `Rect`, `TargetSpec`, `TargetInfo`
- Produces:
  - `win32.get_window_info_by_hwnd(hwnd: int) -> WindowInfo | None`
  - `win32.list_window_infos() -> list[WindowInfo]`
  - `targets.resolve_target(target: int | dict | TargetSpec) -> TargetInfo | None`
  - `targets.list_targets() -> list[TargetInfo]`

- [ ] **Step 1: Write failing target tests**

Create `tests/test_targets.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from windows_input_mcp import targets
from windows_input_mcp.models import Rect, TargetInfo


@dataclass
class FakeWindowInfo:
    hwnd: int
    pid: int
    title: str
    window_rect: tuple[int, int, int, int]
    client_size: tuple[int, int]
    client_screen_origin: tuple[int, int]
    dpi: int
    is_foreground: bool
    is_minimized: bool = False


def test_from_win32_info_builds_client_screen_rect() -> None:
    info = FakeWindowInfo(
        hwnd=10,
        pid=20,
        title="Game",
        window_rect=(90, 80, 500, 400),
        client_size=(320, 200),
        client_screen_origin=(100, 120),
        dpi=144,
        is_foreground=True,
    )

    target = targets.from_win32_info(info)

    assert target == TargetInfo(
        hwnd=10,
        pid=20,
        title="Game",
        window_rect=Rect(90, 80, 500, 400),
        client_rect_screen=Rect(100, 120, 420, 320),
        client_size=(320, 200),
        client_screen_origin=(100, 120),
        dpi=144,
        is_foreground=True,
        is_minimized=False,
    )


def test_resolve_target_prefers_hwnd(monkeypatch) -> None:
    by_hwnd = FakeWindowInfo(11, 22, "By hwnd", (0, 0, 20, 20), (10, 10), (1, 2), 96, False)
    by_pid = FakeWindowInfo(33, 44, "By pid", (0, 0, 40, 40), (20, 20), (3, 4), 96, False)
    monkeypatch.setattr(targets.win32, "get_window_info_by_hwnd", lambda hwnd: by_hwnd)
    monkeypatch.setattr(targets.win32, "get_window_info", lambda pid: by_pid)

    resolved = targets.resolve_target({"pid": 44, "hwnd": 11})

    assert resolved is not None
    assert resolved.hwnd == 11
    assert resolved.pid == 22


def test_list_targets_filters_zero_client_windows(monkeypatch) -> None:
    visible = FakeWindowInfo(1, 2, "Visible", (0, 0, 20, 20), (10, 10), (0, 0), 96, False)
    empty = FakeWindowInfo(3, 4, "Empty", (0, 0, 20, 20), (0, 0), (0, 0), 96, False)
    monkeypatch.setattr(targets.win32, "list_window_infos", lambda: [visible, empty])

    listed = targets.list_targets()

    assert [item.hwnd for item in listed] == [1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run --extra dev pytest tests/test_targets.py -q
```

Expected: FAIL with `ModuleNotFoundError` for `windows_input_mcp.targets`.

- [ ] **Step 3: Extend `windows_input_mcp/win32.py`**

Add `is_minimized` to `WindowInfo`, then add hwnd and listing helpers:

```python
@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    window_rect: tuple[int, int, int, int]
    client_size: tuple[int, int]
    client_screen_origin: tuple[int, int]
    dpi: int
    is_foreground: bool
    is_minimized: bool = False
```

```python
def _window_info_from_hwnd(hwnd: int) -> WindowInfo | None:
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    proc_pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_pid))

    title_len = user32.GetWindowTextLengthW(hwnd)
    title_buf = ctypes.create_unicode_buffer(title_len + 1)
    user32.GetWindowTextW(hwnd, title_buf, title_len + 1)

    wrect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wrect))

    cw, ch = _client_size(hwnd)
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))

    try:
        dpi = user32.GetDpiForWindow(hwnd)
    except (OSError, AttributeError):
        dpi = 96

    return WindowInfo(
        hwnd=hwnd,
        pid=proc_pid.value,
        title=title_buf.value,
        window_rect=(wrect.left, wrect.top, wrect.right, wrect.bottom),
        client_size=(cw, ch),
        client_screen_origin=(pt.x, pt.y),
        dpi=dpi,
        is_foreground=(user32.GetForegroundWindow() == hwnd),
        is_minimized=bool(user32.IsIconic(hwnd)),
    )


def get_window_info_by_hwnd(hwnd: int) -> WindowInfo | None:
    return _window_info_from_hwnd(hwnd)


def list_window_infos() -> list[WindowInfo]:
    infos: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if _class_name(hwnd) in _AUX_WINDOW_CLASSES:
            return True
        info = _window_info_from_hwnd(hwnd)
        if info is not None:
            infos.append(info)
        return True

    user32.EnumWindows(enum_proc, 0)
    return infos
```

Update `get_window_info(pid)` to call `_window_info_from_hwnd(hwnd)` after `find_window_by_pid`.

- [ ] **Step 4: Create `windows_input_mcp/targets.py`**

Add:

```python
from __future__ import annotations

from typing import Any

from . import win32
from .models import Rect, TargetInfo, TargetSpec


def from_win32_info(info: Any) -> TargetInfo:
    window_rect = Rect.from_list(list(info.window_rect))
    cx, cy = info.client_screen_origin
    cw, ch = info.client_size
    client_rect = Rect(cx, cy, cx + cw, cy + ch)
    return TargetInfo(
        hwnd=int(info.hwnd),
        pid=int(info.pid),
        title=str(info.title),
        window_rect=window_rect,
        client_rect_screen=client_rect,
        client_size=(int(cw), int(ch)),
        client_screen_origin=(int(cx), int(cy)),
        dpi=int(info.dpi),
        is_foreground=bool(info.is_foreground),
        is_minimized=bool(getattr(info, "is_minimized", False)),
    )


def resolve_target(target: int | dict[str, Any] | TargetSpec) -> TargetInfo | None:
    spec = TargetSpec.from_value(target)
    info = None
    if spec.hwnd is not None:
        info = win32.get_window_info_by_hwnd(spec.hwnd)
    if info is None and spec.pid is not None:
        info = win32.get_window_info(spec.pid)
    return from_win32_info(info) if info is not None else None


def list_targets() -> list[TargetInfo]:
    out: list[TargetInfo] = []
    for info in win32.list_window_infos():
        target = from_win32_info(info)
        if target.client_size[0] > 0 and target.client_size[1] > 0:
            out.append(target)
    out.sort(key=lambda item: item.client_size[0] * item.client_size[1], reverse=True)
    return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:

```powershell
uv run --extra dev pytest tests/test_targets.py -q
```

Expected: PASS.

- [ ] **Step 6: Run compatibility import check**

Run:

```powershell
uv run --extra dev python -c "from windows_input_mcp import win32; print(win32.WindowInfo)"
```

Expected: prints `WindowInfo` without import errors.

- [ ] **Step 7: Commit**

```powershell
git add windows_input_mcp/win32.py windows_input_mcp/targets.py tests/test_targets.py
git commit -m "feat: add target resolution"
```

---

### Task 4: Coordinate Geometry And Frame Mapping

**Files:**
- Create: `windows_input_mcp/geometry.py`
- Create: `tests/test_geometry.py`

**Interfaces:**
- Consumes: `Rect`, `TargetInfo`
- Produces:
  - `FrameGeometry(image_size: tuple[int, int], capture_rect_screen: Rect, client_rect_screen: Rect, scale: float = 1.0)`
  - `point_to_screen(x, y, scope, target, frame=None, framebuffer_size=None) -> tuple[int, int]`

- [ ] **Step 1: Write failing geometry tests**

Create `tests/test_geometry.py`:

```python
from __future__ import annotations

import pytest

from windows_input_mcp.geometry import FrameGeometry, point_to_screen
from windows_input_mcp.models import Rect, TargetInfo


def _target() -> TargetInfo:
    return TargetInfo(
        hwnd=1,
        pid=2,
        title="Game",
        window_rect=Rect(90, 80, 500, 400),
        client_rect_screen=Rect(100, 120, 420, 320),
        client_size=(320, 200),
        client_screen_origin=(100, 120),
        dpi=144,
        is_foreground=True,
    )


def test_client_to_screen() -> None:
    assert point_to_screen(10, 20, "client", _target()) == (110, 140)


def test_screen_passthrough() -> None:
    assert point_to_screen(-50, 25, "screen", _target()) == (-50, 25)


def test_capture_to_screen_uses_frame_geometry() -> None:
    frame = FrameGeometry(
        image_size=(160, 100),
        capture_rect_screen=Rect(100, 120, 420, 320),
        client_rect_screen=Rect(100, 120, 420, 320),
    )

    assert point_to_screen(80, 50, "capture", _target(), frame=frame) == (260, 220)


def test_normalized_to_screen_uses_frame_geometry() -> None:
    frame = FrameGeometry(
        image_size=(160, 100),
        capture_rect_screen=Rect(100, 120, 420, 320),
        client_rect_screen=Rect(100, 120, 420, 320),
    )

    assert point_to_screen(0.5, 0.5, "normalized", _target(), frame=frame) == (260, 220)


def test_framebuffer_alias_uses_explicit_size_without_frame() -> None:
    assert point_to_screen(80, 50, "framebuffer", _target(), framebuffer_size=(160, 100)) == (260, 220)


def test_capture_without_frame_raises_clear_error() -> None:
    with pytest.raises(ValueError, match="frame geometry"):
        point_to_screen(1, 2, "capture", _target())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run --extra dev pytest tests/test_geometry.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'windows_input_mcp.geometry'`.

- [ ] **Step 3: Create `windows_input_mcp/geometry.py`**

Add:

```python
from __future__ import annotations

from dataclasses import dataclass

from .models import Rect, TargetInfo


@dataclass(frozen=True)
class FrameGeometry:
    image_size: tuple[int, int]
    capture_rect_screen: Rect
    client_rect_screen: Rect
    scale: float = 1.0

    def capture_to_screen(self, x: float, y: float) -> tuple[int, int]:
        iw, ih = self.image_size
        if iw <= 0 or ih <= 0:
            raise ValueError("frame image size must be positive")
        sx = self.capture_rect_screen.left + (float(x) * self.capture_rect_screen.width / iw)
        sy = self.capture_rect_screen.top + (float(y) * self.capture_rect_screen.height / ih)
        return int(round(sx)), int(round(sy))

    def normalized_to_screen(self, x: float, y: float) -> tuple[int, int]:
        return self.capture_to_screen(float(x) * self.image_size[0], float(y) * self.image_size[1])


def point_to_screen(
    x: float,
    y: float,
    scope: str,
    target: TargetInfo,
    *,
    frame: FrameGeometry | None = None,
    framebuffer_size: tuple[int, int] | None = None,
) -> tuple[int, int]:
    normalized_scope = scope.lower().strip()
    if normalized_scope == "screen":
        return int(round(x)), int(round(y))
    if normalized_scope == "client":
        return (
            int(round(target.client_rect_screen.left + x)),
            int(round(target.client_rect_screen.top + y)),
        )
    if normalized_scope == "normalized":
        if frame is None:
            raise ValueError("normalized scope requires frame geometry")
        return frame.normalized_to_screen(x, y)
    if normalized_scope == "capture":
        if frame is None:
            raise ValueError("capture scope requires frame geometry")
        return frame.capture_to_screen(x, y)
    if normalized_scope == "framebuffer":
        if frame is not None:
            return frame.capture_to_screen(x, y)
        if framebuffer_size is None:
            return point_to_screen(x, y, "client", target)
        fw, fh = framebuffer_size
        if fw <= 0 or fh <= 0:
            raise ValueError("framebuffer size must be positive")
        cx = float(x) * target.client_rect_screen.width / fw
        cy = float(y) * target.client_rect_screen.height / fh
        return point_to_screen(cx, cy, "client", target)
    raise ValueError(f"unknown coordinate scope: {scope}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```powershell
uv run --extra dev pytest tests/test_geometry.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add windows_input_mcp/geometry.py tests/test_geometry.py
git commit -m "feat: add game coordinate geometry"
```

---

### Task 5: File-Backed Frame Cache

**Files:**
- Create: `windows_input_mcp/frames.py`
- Create: `tests/test_frames.py`

**Interfaces:**
- Consumes: `FrameGeometry`, `Rect`, `TargetInfo`
- Produces:
  - `FrameRecord(frame_id, image_path, metadata_path, metadata, created_at)`
  - `FrameCache.store(image, metadata) -> FrameRecord`
  - `FrameCache.get(frame_id) -> FrameRecord | None`
  - `FrameCache.cleanup() -> int`

- [ ] **Step 1: Write failing frame cache tests**

Create `tests/test_frames.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run --extra dev pytest tests/test_frames.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'windows_input_mcp.frames'`.

- [ ] **Step 3: Create `windows_input_mcp/frames.py`**

Add:

```python
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any
from uuid import uuid4

from PIL import Image


def default_frame_cache_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "windows-input-mcp"
    return base / "frames"


@dataclass(frozen=True)
class FrameRecord:
    frame_id: str
    image_path: Path
    metadata_path: Path
    metadata: dict[str, Any]
    created_at: float


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

    def store(self, image: Image.Image, metadata: dict[str, Any]) -> FrameRecord:
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
        metadata_path.write_text(json.dumps(enriched, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        return FrameRecord(frame_id, image_path, metadata_path, enriched, created_at)

    def get(self, frame_id: str) -> FrameRecord | None:
        image_path = self.directory / f"{frame_id}.png"
        metadata_path = self.directory / f"{frame_id}.json"
        if not image_path.exists() or not metadata_path.exists():
            return None
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        created_at = float(metadata.get("created_at", 0.0))
        if self._now() - created_at > self.ttl_sec:
            return None
        return FrameRecord(frame_id, image_path, metadata_path, metadata, created_at)

    def cleanup(self) -> int:
        removed = 0
        for metadata_path in self.directory.glob("frame_*.json"):
            frame_id = metadata_path.stem
            record = self.get(frame_id)
            if record is not None:
                continue
            image_path = self.directory / f"{frame_id}.png"
            for path in (metadata_path, image_path):
                if path.exists():
                    path.unlink()
                    removed += 1
        return removed
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```powershell
uv run --extra dev pytest tests/test_frames.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add windows_input_mcp/frames.py tests/test_frames.py
git commit -m "feat: add frame cache"
```

---

### Task 6: Capture Backend Registry And Backends

**Files:**
- Create: `windows_input_mcp/capture/__init__.py`
- Create: `windows_input_mcp/capture/base.py`
- Create: `windows_input_mcp/capture/pillow_backend.py`
- Create: `windows_input_mcp/capture/mss_backend.py`
- Create: `windows_input_mcp/capture/dxcam_backend.py`
- Create: `tests/test_capture_backends.py`

**Interfaces:**
- Consumes: `Rect`
- Produces:
  - `CaptureResult(image: PIL.Image.Image, backend: str, mode: str)`
  - `CaptureBackend.registry`
  - `capture_region(rect, backend="auto") -> CaptureResult`

- [ ] **Step 1: Write failing capture backend tests**

Create `tests/test_capture_backends.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PIL import Image

import windows_input_mcp.capture.base as base
from windows_input_mcp.capture.base import CaptureBackend, capture_region
from windows_input_mcp.models import Rect


@pytest.fixture(autouse=True)
def isolate_backend_registry(monkeypatch):
    original = dict(CaptureBackend.registry)
    monkeypatch.setattr(CaptureBackend, "registry", dict(original))


def test_backend_subclass_registers_by_name() -> None:
    class DummyBackend(CaptureBackend):
        name = "dummy"
        priority = 1

        def is_available(self, rect):
            return True

        def capture(self, rect):
            return Image.new("RGB", (1, 1), "white")

    assert CaptureBackend.registry["dummy"] is DummyBackend


def test_auto_capture_uses_priority_order(monkeypatch) -> None:
    calls: list[str] = []

    class SlowBackend(CaptureBackend):
        name = "slow"
        priority = 20

        def is_available(self, rect):
            return True

        def capture(self, rect):
            calls.append("slow")
            return Image.new("RGB", (1, 1), "red")

    class FastBackend(CaptureBackend):
        name = "fast"
        priority = 10

        def is_available(self, rect):
            return True

        def capture(self, rect):
            calls.append("fast")
            return Image.new("RGB", (2, 2), "blue")

    monkeypatch.setattr(base, "_backend_instances", {})

    result = capture_region(Rect(0, 0, 10, 10), backend="auto")

    assert result.backend == "fast"
    assert result.image.size == (2, 2)
    assert calls == ["fast"]


def test_auto_capture_falls_back_after_failure(monkeypatch) -> None:
    class BrokenBackend(CaptureBackend):
        name = "broken"
        priority = 1

        def is_available(self, rect):
            return True

        def capture(self, rect):
            raise RuntimeError("broken")

    class GoodBackend(CaptureBackend):
        name = "good"
        priority = 2

        def is_available(self, rect):
            return True

        def capture(self, rect):
            return Image.new("RGB", (3, 3), "green")

    monkeypatch.setattr(base, "_backend_instances", {})

    result = capture_region(Rect(0, 0, 10, 10), backend="auto")

    assert result.backend == "good"
    assert result.image.size == (3, 3)


def test_unknown_backend_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown capture backend"):
        capture_region(Rect(0, 0, 10, 10), backend="missing")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run --extra dev pytest tests/test_capture_backends.py -q
```

Expected: FAIL because `windows_input_mcp.capture.base` does not exist.

- [ ] **Step 3: Create `windows_input_mcp/capture/base.py`**

Add:

```python
from __future__ import annotations

import logging
from dataclasses import dataclass

from PIL import Image

from windows_input_mcp.models import Rect

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptureResult:
    image: Image.Image
    backend: str
    mode: str


class CaptureBackend:
    name: str
    priority: int
    registry: dict[str, type["CaptureBackend"]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "name" in cls.__dict__ and "priority" in cls.__dict__:
            existing = CaptureBackend.registry.get(cls.name)
            if existing is not None and existing is not cls:
                raise ValueError(f"Duplicate capture backend name: {cls.name!r}")
            CaptureBackend.registry[cls.name] = cls

    def is_available(self, rect: Rect | None) -> bool:
        return True

    def capture(self, rect: Rect | None) -> Image.Image:
        raise NotImplementedError

    def mode(self, rect: Rect | None) -> str:
        return "region" if rect is not None else "virtual_screen"


_backend_instances: dict[str, CaptureBackend] = {}


def _get_backend(name: str) -> CaptureBackend:
    if name not in _backend_instances:
        cls = CaptureBackend.registry.get(name)
        if cls is None:
            raise ValueError(f"Unknown capture backend: {name}")
        _backend_instances[name] = cls()
    return _backend_instances[name]


def _candidate_classes(selected: str) -> list[type[CaptureBackend]]:
    if selected == "auto":
        return sorted(CaptureBackend.registry.values(), key=lambda cls: cls.priority)
    cls = CaptureBackend.registry.get(selected)
    if cls is None:
        raise ValueError(f"Unknown capture backend: {selected}")
    return [cls]


def capture_region(rect: Rect | None, *, backend: str = "auto") -> CaptureResult:
    selected = backend.strip().lower()
    last_error: Exception | None = None
    for cls in _candidate_classes(selected):
        inst = _get_backend(cls.name)
        if not inst.is_available(rect):
            continue
        try:
            image = inst.capture(rect)
            return CaptureResult(image=image, backend=inst.name, mode=inst.mode(rect))
        except (OSError, RuntimeError, ValueError) as exc:
            last_error = exc
            log.warning("capture backend %s failed", inst.name, exc_info=selected != "auto")
    if last_error is not None:
        raise RuntimeError(f"all capture backends failed; last error: {last_error}") from last_error
    raise RuntimeError("no capture backend is available")
```

- [ ] **Step 4: Create backend modules and package exports**

Add `windows_input_mcp/capture/pillow_backend.py`:

```python
from __future__ import annotations

from PIL import Image, ImageGrab

from windows_input_mcp.models import Rect

from .base import CaptureBackend


class PillowBackend(CaptureBackend):
    name = "pillow"
    priority = 100

    def capture(self, rect: Rect | None) -> Image.Image:
        kwargs: dict[str, object] = {"all_screens": True}
        if rect is not None:
            kwargs["bbox"] = (rect.left, rect.top, rect.right, rect.bottom)
        return ImageGrab.grab(**kwargs)
```

Add `windows_input_mcp/capture/mss_backend.py`:

```python
from __future__ import annotations

from PIL import Image

from windows_input_mcp.models import Rect

from .base import CaptureBackend

try:
    import mss
except ImportError:
    mss = None


class MssBackend(CaptureBackend):
    name = "mss"
    priority = 20

    def is_available(self, rect: Rect | None) -> bool:
        return mss is not None

    def capture(self, rect: Rect | None) -> Image.Image:
        if mss is None:
            raise RuntimeError("mss is not available")
        with mss.mss() as sct:
            monitor = sct.monitors[0] if rect is None else {
                "left": rect.left,
                "top": rect.top,
                "width": rect.width,
                "height": rect.height,
            }
            raw = sct.grab(monitor)
            return Image.frombytes("RGB", raw.size, raw.rgb)
```

Add `windows_input_mcp/capture/dxcam_backend.py`:

```python
from __future__ import annotations

from PIL import Image

from windows_input_mcp import win32
from windows_input_mcp.models import Rect

from .base import CaptureBackend

try:
    import dxcam
except Exception:
    dxcam = None


class DxcamBackend(CaptureBackend):
    name = "dxcam"
    priority = 10

    def __init__(self) -> None:
        self._cameras: dict[int, object] = {}

    def is_available(self, rect: Rect | None) -> bool:
        return dxcam is not None and rect is not None and self._resolve_region(rect) is not None

    def _resolve_region(self, rect: Rect) -> tuple[int, tuple[int, int, int, int] | None] | None:
        for index, monitor in enumerate(win32.get_monitor_rects()):
            if monitor.left <= rect.left and monitor.top <= rect.top and monitor.right >= rect.right and monitor.bottom >= rect.bottom:
                if monitor == rect:
                    return index, None
                return index, (
                    rect.left - monitor.left,
                    rect.top - monitor.top,
                    rect.right - monitor.left,
                    rect.bottom - monitor.top,
                )
        return None

    def _camera(self, output_idx: int) -> object:
        if output_idx not in self._cameras:
            self._cameras[output_idx] = dxcam.create(output_idx=output_idx, processor_backend="numpy")
        return self._cameras[output_idx]

    def capture(self, rect: Rect | None) -> Image.Image:
        if rect is None:
            raise ValueError("dxcam backend requires a region")
        resolved = self._resolve_region(rect)
        if resolved is None:
            raise ValueError("dxcam region must be contained in one monitor")
        output_idx, region = resolved
        frame = self._camera(output_idx).grab(region=region, copy=True, new_frame_only=False)
        if frame is None:
            raise RuntimeError("dxcam returned no frame")
        return Image.fromarray(frame)
```

Add `windows_input_mcp/capture/__init__.py`:

```python
from . import dxcam_backend, mss_backend, pillow_backend
from .base import CaptureBackend, CaptureResult, capture_region

__all__ = ["CaptureBackend", "CaptureResult", "capture_region"]
```

Add this helper to `windows_input_mcp/win32.py`:

```python
def get_monitor_rects():
    from .models import Rect

    monitors = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def enum_proc(hmonitor, hdc, rect, lparam):
        monitors.append(Rect(rect.contents.left, rect.contents.top, rect.contents.right, rect.contents.bottom))
        return True

    user32.EnumDisplayMonitors(None, None, enum_proc, 0)
    return monitors
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:

```powershell
uv run --extra dev pytest tests/test_capture_backends.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add windows_input_mcp/capture windows_input_mcp/win32.py tests/test_capture_backends.py
git commit -m "feat: add capture backend registry"
```

---

### Task 7: Target-Aware Capture Service

**Files:**
- Create: `windows_input_mcp/capture/service.py`
- Create: `tests/test_capture_service.py`

**Interfaces:**
- Consumes: `targets.resolve_target`, `capture_region`, `FrameCache`, `FrameGeometry`
- Produces:
  - `capture_target(target, region=None, scope="client", backend="auto", max_width=1920, cache=None) -> dict`
  - metadata dictionary with `frame_id`, `image_path`, `target`, `image`, `geometry`, and `backend`

- [ ] **Step 1: Write failing service tests**

Create `tests/test_capture_service.py`:

```python
from __future__ import annotations

from PIL import Image

from windows_input_mcp.capture import service
from windows_input_mcp.frames import FrameCache
from windows_input_mcp.models import Rect, TargetInfo


def _target() -> TargetInfo:
    return TargetInfo(
        hwnd=1,
        pid=2,
        title="Game",
        window_rect=Rect(90, 80, 500, 400),
        client_rect_screen=Rect(100, 120, 420, 320),
        client_size=(320, 200),
        client_screen_origin=(100, 120),
        dpi=144,
        is_foreground=True,
    )


def test_capture_target_stores_frame_and_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(
        service,
        "capture_region",
        lambda rect, backend: service.CaptureResult(Image.new("RGB", (320, 200), "red"), "fake", "region"),
    )
    cache = FrameCache(tmp_path, ttl_sec=60)

    result = service.capture_target({"pid": 2}, backend="fake", cache=cache)

    assert result["success"] is True
    assert result["frame_id"].startswith("frame_")
    assert result["image"]["width"] == 320
    assert result["image"]["height"] == 200
    assert result["geometry"]["capture_rect_screen"] == [100, 120, 420, 320]
    assert result["backend"]["name"] == "fake"
    assert cache.get(result["frame_id"]) is not None


def test_capture_target_returns_structured_error_for_missing_target(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service.targets, "resolve_target", lambda target: None)

    result = service.capture_target({"pid": 2}, cache=FrameCache(tmp_path))

    assert result["success"] is False
    assert result["error_code"] == "TARGET_NOT_FOUND"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run --extra dev pytest tests/test_capture_service.py -q
```

Expected: FAIL because `windows_input_mcp.capture.service` does not exist.

- [ ] **Step 3: Create `windows_input_mcp/capture/service.py`**

Add:

```python
from __future__ import annotations

from PIL import Image

from windows_input_mcp import targets
from windows_input_mcp.frames import FrameCache
from windows_input_mcp.geometry import FrameGeometry
from windows_input_mcp.models import Rect, TargetInfo, error_response, ok_response

from .base import CaptureResult, capture_region


def _capture_rect(target: TargetInfo, region: list[int] | None, scope: str) -> Rect:
    if region is None:
        return target.client_rect_screen
    rect = Rect.from_list(region)
    if scope == "screen":
        return rect
    if scope == "client":
        return Rect(
            target.client_rect_screen.left + rect.left,
            target.client_rect_screen.top + rect.top,
            target.client_rect_screen.left + rect.right,
            target.client_rect_screen.top + rect.bottom,
        )
    raise ValueError("capture region scope must be client or screen")


def _resize_if_needed(image: Image.Image, max_width: int) -> tuple[Image.Image, float]:
    if max_width <= 0 or image.width <= max_width:
        return image, 1.0
    scale = max_width / image.width
    height = max(1, int(round(image.height * scale)))
    return image.resize((max_width, height), Image.LANCZOS), scale


def capture_target(
    target: int | dict,
    *,
    region: list[int] | None = None,
    scope: str = "client",
    backend: str = "auto",
    max_width: int = 1920,
    cache: FrameCache | None = None,
) -> dict:
    resolved = targets.resolve_target(target)
    if resolved is None:
        return error_response("TARGET_NOT_FOUND", "Target window was not found", retryable=True, target=target)
    if resolved.is_minimized:
        return error_response("TARGET_MINIMIZED", "Target window is minimized and cannot be captured", retryable=True, **resolved.to_dict())

    cache = cache or FrameCache()
    rect = _capture_rect(resolved, region, scope)
    captured = capture_region(rect, backend=backend)
    image, scale = _resize_if_needed(captured.image, max_width)
    frame_geometry = FrameGeometry(
        image_size=(image.width, image.height),
        capture_rect_screen=rect,
        client_rect_screen=resolved.client_rect_screen,
        scale=scale,
    )
    metadata = {
        "target": resolved.to_dict(),
        "image": {"width": image.width, "height": image.height, "format": "png", "scale": scale},
        "geometry": {
            "window_rect_screen": resolved.window_rect.to_list(),
            "client_rect_screen": resolved.client_rect_screen.to_list(),
            "capture_rect_screen": rect.to_list(),
            "client_size": list(resolved.client_size),
            "dpi": resolved.dpi,
            "frame_image_size": list(frame_geometry.image_size),
        },
        "backend": {"name": captured.backend, "mode": captured.mode},
    }
    record = cache.store(image, metadata)
    return ok_response(
        frame_id=record.frame_id,
        image_path=str(record.image_path),
        metadata_path=str(record.metadata_path),
        **record.metadata,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```powershell
uv run --extra dev pytest tests/test_capture_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add windows_input_mcp/capture/service.py tests/test_capture_service.py
git commit -m "feat: add target capture service"
```

---

### Task 8: Daemon Capture And Frame-Aware Mouse Handlers

**Files:**
- Modify: `windows_input_mcp/daemon.py`
- Create: `tests/test_daemon_handlers.py`

**Interfaces:**
- Consumes: `capture.service.capture_target`, `FrameCache`, `geometry.point_to_screen`, `targets.resolve_target`
- Produces daemon methods:
  - `list_targets`
  - `get_target_info`
  - `focus_target`
  - `capture`
  - frame-aware `mouse_click`, `mouse_drag`, and `scroll`

- [ ] **Step 1: Write failing daemon handler tests**

Create `tests/test_daemon_handlers.py`:

```python
from __future__ import annotations

from windows_input_mcp import daemon
from windows_input_mcp.models import Rect, TargetInfo


def _target() -> TargetInfo:
    return TargetInfo(
        hwnd=1,
        pid=2,
        title="Game",
        window_rect=Rect(90, 80, 500, 400),
        client_rect_screen=Rect(100, 120, 420, 320),
        client_size=(320, 200),
        client_screen_origin=(100, 120),
        dpi=144,
        is_foreground=True,
    )


def test_list_targets_handler(monkeypatch) -> None:
    monkeypatch.setattr(daemon.targets, "list_targets", lambda: [_target()])

    result = daemon._h_list_targets({})

    assert result["success"] is True
    assert result["targets"][0]["pid"] == 2


def test_capture_handler_delegates_to_capture_service(monkeypatch) -> None:
    monkeypatch.setattr(
        daemon.capture_service,
        "capture_target",
        lambda **kwargs: {"success": True, "frame_id": "frame_1"},
    )

    result = daemon._h_capture({"target": {"pid": 2}, "backend": "fake"})

    assert result == {"success": True, "frame_id": "frame_1"}


def test_focus_target_handler_resolves_target(monkeypatch) -> None:
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(daemon.win32, "focus_window_detailed", lambda pid: {"success": True, "hwnd": 1})

    result = daemon._h_focus_target({"target": {"pid": 2}})

    assert result["success"] is True
    assert result["pid"] == 2


def test_mouse_click_uses_frame_geometry_when_frame_id_present(monkeypatch) -> None:
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(daemon.win32, "focus_window", lambda pid: True)
    monkeypatch.setattr(daemon.win32, "send_mouse_click", lambda sx, sy, button, clicks: True)

    class FakeCache:
        def get(self, frame_id):
            return type("Record", (), {
                "metadata": {
                    "geometry": {
                        "capture_rect_screen": [100, 120, 420, 320],
                        "client_rect_screen": [100, 120, 420, 320],
                    },
                    "image": {"width": 160, "height": 100},
                }
            })()

    monkeypatch.setattr(daemon, "FRAME_CACHE", FakeCache())

    result = daemon._h_mouse_click({
        "target": {"pid": 2},
        "x": 80,
        "y": 50,
        "scope": "capture",
        "frame_id": "frame_1",
    })

    assert result["success"] is True
    assert result["screen_coords"] == [260, 220]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run --extra dev pytest tests/test_daemon_handlers.py -q
```

Expected: FAIL because new daemon handler names and imports are absent.

- [ ] **Step 3: Update daemon imports and cache singleton**

At the top of `windows_input_mcp/daemon.py`, add:

```python
from . import targets
from .capture import service as capture_service
from .frames import FrameCache
from .geometry import FrameGeometry, point_to_screen
from .models import Rect, error_response, ok_response

FRAME_CACHE = FrameCache()
```

- [ ] **Step 4: Add target and capture handlers**

Add:

```python
def _h_list_targets(p: dict) -> dict:
    return ok_response(targets=[target.to_dict() for target in targets.list_targets()])


def _h_get_target_info(p: dict) -> dict:
    target = targets.resolve_target(p.get("target", p.get("pid")))
    if target is None:
        return error_response("TARGET_NOT_FOUND", "Target window was not found", retryable=True, params=p)
    return ok_response(target=target.to_dict())


def _h_focus_target(p: dict) -> dict:
    target = targets.resolve_target(p.get("target", p.get("pid")))
    if target is None:
        return error_response("TARGET_NOT_FOUND", "Target window was not found", retryable=True, params=p)
    out = win32.focus_window_detailed(target.pid)
    out["pid"] = target.pid
    out["target"] = target.to_dict()
    return out


def _h_capture(p: dict) -> dict:
    return capture_service.capture_target(
        target=p.get("target", p.get("pid")),
        region=p.get("region"),
        scope=p.get("scope", "client"),
        backend=p.get("backend", "auto"),
        max_width=p.get("max_width", 1920),
        cache=FRAME_CACHE,
    )
```

- [ ] **Step 5: Add frame geometry helper and update mouse handler target parsing**

Add:

```python
def _target_param(p: dict):
    return p.get("target", p.get("pid"))


def _frame_geometry(frame_id: str | None) -> FrameGeometry | None:
    if not frame_id:
        return None
    record = FRAME_CACHE.get(frame_id)
    if record is None:
        raise KeyError(frame_id)
    geometry = record.metadata["geometry"]
    image = record.metadata["image"]
    return FrameGeometry(
        image_size=(int(image["width"]), int(image["height"])),
        capture_rect_screen=Rect.from_list(geometry["capture_rect_screen"]),
        client_rect_screen=Rect.from_list(geometry["client_rect_screen"]),
        scale=float(image.get("scale", 1.0)),
    )
```

Replace the start of `_h_mouse_click` with target-aware resolution:

```python
def _h_mouse_click(p: dict) -> dict:
    target = targets.resolve_target(_target_param(p))
    if target is None:
        return error_response("TARGET_NOT_FOUND", "Target window was not found", retryable=True, params=p)
    if p.get("activate", True):
        win32.focus_window(target.pid)
        time.sleep(0.05)
    try:
        frame = _frame_geometry(p.get("frame_id"))
    except KeyError:
        return error_response("FRAME_NOT_FOUND", "Frame metadata was not found", retryable=True, frame_id=p.get("frame_id"))
    sx, sy = point_to_screen(
        p["x"],
        p["y"],
        p.get("scope", "capture" if p.get("frame_id") else "framebuffer"),
        target,
        frame=frame,
        framebuffer_size=_fb_size(p),
    )
    ok = win32.send_mouse_click(sx, sy, button=p.get("button", "left"), clicks=p.get("clicks", 1))
    return ok_response(
        sent=ok,
        screen_coords=[sx, sy],
        scope=p.get("scope", "capture" if p.get("frame_id") else "framebuffer"),
        translated_from=[p["x"], p["y"]],
        target=target.to_dict(),
    )
```

Apply the same target, frame, and `point_to_screen` pattern to `_h_mouse_drag` and `_h_scroll`.

- [ ] **Step 6: Register new handlers**

Update `HANDLERS`:

```python
HANDLERS = {
    "list_targets": _h_list_targets,
    "get_target_info": _h_get_target_info,
    "focus_target": _h_focus_target,
    "capture": _h_capture,
    "get_window_info": _h_get_window_info,
    "focus_window": _h_focus_window,
    "mouse_click": _h_mouse_click,
    "mouse_drag": _h_mouse_drag,
    "scroll": _h_scroll,
    "send_keys": _h_send_keys,
}
```

- [ ] **Step 7: Run daemon handler tests**

Run:

```powershell
uv run --extra dev pytest tests/test_daemon_handlers.py -q
```

Expected: PASS.

- [ ] **Step 8: Run existing compatibility import**

Run:

```powershell
uv run --extra dev python -c "from windows_input_mcp.daemon import HANDLERS; print(sorted(HANDLERS))"
```

Expected: output includes `capture`, `focus_target`, `list_targets`, `mouse_click`, and `send_keys`.

- [ ] **Step 9: Commit**

```powershell
git add windows_input_mcp/daemon.py tests/test_daemon_handlers.py
git commit -m "feat: add daemon capture handlers"
```

---

### Task 9: MCP Server Tools

**Files:**
- Modify: `windows_input_mcp/server.py`
- Create: `tests/test_server_tools.py`

**Interfaces:**
- Consumes: daemon methods added in Task 8
- Produces MCP tools:
  - `list_targets()`
  - `get_target_info(target)`
  - `focus_target(target)`
  - `capture(target, region=None, scope="client", backend="auto", max_width=1920)`
  - existing compatibility tools still available

- [ ] **Step 1: Write failing server tool tests**

Create `tests/test_server_tools.py`:

```python
from __future__ import annotations

from windows_input_mcp import server


def test_list_targets_calls_daemon(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server, "_call", lambda method, **params: calls.append((method, params)) or {"success": True})

    assert server.list_targets() == {"success": True}
    assert calls == [("list_targets", {})]


def test_capture_passes_target_and_options(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server, "_call", lambda method, **params: calls.append((method, params)) or {"success": True})

    result = server.capture({"pid": 2}, backend="pillow", max_width=800)

    assert result == {"success": True}
    assert calls == [("capture", {"target": {"pid": 2}, "region": None, "scope": "client", "backend": "pillow", "max_width": 800})]


def test_focus_target_passes_target(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server, "_call", lambda method, **params: calls.append((method, params)) or {"success": True})

    assert server.focus_target({"pid": 2}) == {"success": True}
    assert calls == [("focus_target", {"target": {"pid": 2}})]


def test_compat_mouse_click_still_uses_pid() -> None:
    result = server.mouse_click(pid=2, x=1, y=2, activate=False)

    assert isinstance(result, dict)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run --extra dev pytest tests/test_server_tools.py -q
```

Expected: FAIL because `list_targets` and `capture` are absent.

- [ ] **Step 3: Add MCP tools to `windows_input_mcp/server.py`**

Add imports:

```python
from typing import Any, Literal
```

Add tools before compatibility mouse tools:

```python
@mcp.tool()
def list_targets() -> dict:
    """List visible target windows that can be captured or driven."""
    return _call("list_targets")


@mcp.tool()
def get_target_info(target: dict[str, Any]) -> dict:
    """Return resolved target metadata for a pid/hwnd target object."""
    return _call("get_target_info", target=target)


@mcp.tool()
def focus_target(target: dict[str, Any]) -> dict:
    """Bring a pid/hwnd target object to foreground."""
    return _call("focus_target", target=target)


@mcp.tool()
def capture(
    target: dict[str, Any],
    region: list[int] | None = None,
    scope: Literal["client", "screen"] = "client",
    backend: Literal["auto", "dxcam", "mss", "pillow"] = "auto",
    max_width: int = 1920,
) -> dict:
    """Capture target pixels and return frame metadata plus image_path."""
    return _call(
        "capture",
        target=target,
        region=region,
        scope=scope,
        backend=backend,
        max_width=max_width,
    )
```

Update compatibility mouse tool `scope` literals to include `capture` and add optional `frame_id`:

```python
scope: Literal["framebuffer", "capture", "client", "screen"] = "framebuffer",
frame_id: str | None = None,
```

Pass `target={"pid": pid}` and `frame_id=frame_id` into `_call` while still sending `pid=pid` for compatibility.

- [ ] **Step 4: Run server tool tests**

Run:

```powershell
uv run --extra dev pytest tests/test_server_tools.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add windows_input_mcp/server.py tests/test_server_tools.py
git commit -m "feat: expose capture mcp tools"
```

---

### Task 10: Scan-Code Keyboard Controls

**Files:**
- Create: `windows_input_mcp/input/__init__.py`
- Create: `windows_input_mcp/input/keys.py`
- Modify: `windows_input_mcp/win32.py`
- Modify: `windows_input_mcp/daemon.py`
- Modify: `windows_input_mcp/server.py`
- Create: `tests/test_keys.py`

**Interfaces:**
- Consumes: existing `SendInput` structures in `win32.py`
- Produces:
  - `resolve_key(key: str, mode: str) -> KeyStroke`
  - `win32.send_key_down(key, mode="scancode") -> int`
  - `win32.send_key_up(key, mode="scancode") -> int`
  - `win32.tap_key(key, mode="scancode", hold_ms=30) -> int`
  - daemon methods `key_down`, `key_up`, `tap_key`, `hotkey`
  - MCP tools `key_down`, `key_up`, `tap_key`, `hotkey`, `type_text`

- [ ] **Step 1: Write failing key mapping tests**

Create `tests/test_keys.py`:

```python
from __future__ import annotations

import pytest

from windows_input_mcp.input.keys import resolve_key


def test_resolve_wasd_as_scancode() -> None:
    key = resolve_key("w", "scancode")

    assert key.scan_code == 0x11
    assert key.vk is None
    assert key.extended is False


def test_resolve_arrow_as_extended_scancode() -> None:
    key = resolve_key("left", "scancode")

    assert key.scan_code == 0x4B
    assert key.extended is True


def test_resolve_enter_as_virtual_key() -> None:
    key = resolve_key("enter", "vk")

    assert key.vk == 0x0D
    assert key.scan_code is None


def test_unknown_key_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported key"):
        resolve_key("not-a-key", "scancode")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run --extra dev pytest tests/test_keys.py -q
```

Expected: FAIL because `windows_input_mcp.input.keys` does not exist.

- [ ] **Step 3: Create key mapping files**

Create `windows_input_mcp/input/keys.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KeyStroke:
    key: str
    vk: int | None = None
    scan_code: int | None = None
    extended: bool = False


SCAN_CODES = {
    "w": (0x11, False),
    "a": (0x1E, False),
    "s": (0x1F, False),
    "d": (0x20, False),
    "space": (0x39, False),
    "shift": (0x2A, False),
    "ctrl": (0x1D, False),
    "alt": (0x38, False),
    "enter": (0x1C, False),
    "esc": (0x01, False),
    "escape": (0x01, False),
    "tab": (0x0F, False),
    "left": (0x4B, True),
    "right": (0x4D, True),
    "up": (0x48, True),
    "down": (0x50, True),
}
for index in range(1, 13):
    SCAN_CODES[f"f{index}"] = (0x3A + index, False)

VIRTUAL_KEYS = {
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "tab": 0x09,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
}
for index in range(1, 13):
    VIRTUAL_KEYS[f"f{index}"] = 0x6F + index


def resolve_key(key: str, mode: str = "scancode") -> KeyStroke:
    normalized = key.strip().lower()
    if mode == "scancode":
        item = SCAN_CODES.get(normalized)
        if item is None:
            raise ValueError(f"unsupported key for scancode mode: {key}")
        scan_code, extended = item
        return KeyStroke(key=normalized, scan_code=scan_code, extended=extended)
    if mode == "vk":
        vk = VIRTUAL_KEYS.get(normalized)
        if vk is None and len(normalized) == 1:
            vk = ord(normalized.upper())
        if vk is None:
            raise ValueError(f"unsupported key for vk mode: {key}")
        return KeyStroke(key=normalized, vk=vk)
    raise ValueError("mode must be scancode or vk")
```

Create `windows_input_mcp/input/__init__.py`:

```python
from .keys import KeyStroke, resolve_key

__all__ = ["KeyStroke", "resolve_key"]
```

- [ ] **Step 4: Extend `windows_input_mcp/win32.py` key primitives**

Add imports:

```python
from .input.keys import KeyStroke, resolve_key
```

Add constants:

```python
KEYEVENTF_EXTENDEDKEY = 0x0001
```

Add functions:

```python
def _keystroke_input(stroke: KeyStroke, key_up: bool) -> INPUT:
    flags = KEYEVENTF_KEYUP if key_up else 0
    if stroke.scan_code is not None:
        flags |= KEYEVENTF_SCANCODE
        if stroke.extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        return INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=KEYBDINPUT(
            wVk=0, wScan=stroke.scan_code, dwFlags=flags, time=0, dwExtraInfo=None
        )))
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=KEYBDINPUT(
        wVk=stroke.vk or 0, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None
    )))


def send_key_down(key: str, mode: str = "scancode") -> int:
    stroke = resolve_key(key, mode)
    return _send_inputs([_keystroke_input(stroke, key_up=False)])


def send_key_up(key: str, mode: str = "scancode") -> int:
    stroke = resolve_key(key, mode)
    return _send_inputs([_keystroke_input(stroke, key_up=True)])


def tap_key(key: str, mode: str = "scancode", hold_ms: int = 30) -> int:
    stroke = resolve_key(key, mode)
    sent = _send_inputs([_keystroke_input(stroke, key_up=False)])
    if hold_ms > 0:
        import time
        time.sleep(hold_ms / 1000.0)
    sent += _send_inputs([_keystroke_input(stroke, key_up=True)])
    return sent
```

- [ ] **Step 5: Add daemon and server tools**

In `daemon.py`, add handlers:

```python
def _focus_if_requested(target_param, activate: bool) -> tuple[bool, dict | None]:
    if not activate:
        return True, None
    target = targets.resolve_target(target_param)
    if target is None:
        return False, error_response("TARGET_NOT_FOUND", "Target window was not found", retryable=True)
    return win32.focus_window(target.pid), None


def _h_key_down(p: dict) -> dict:
    ok, error = _focus_if_requested(_target_param(p), p.get("activate", True))
    if error:
        return error
    sent = win32.send_key_down(p["key"], p.get("mode", "scancode"))
    return ok_response(sent=sent, key=p["key"], mode=p.get("mode", "scancode"), focused=ok)


def _h_key_up(p: dict) -> dict:
    ok, error = _focus_if_requested(_target_param(p), p.get("activate", True))
    if error:
        return error
    sent = win32.send_key_up(p["key"], p.get("mode", "scancode"))
    return ok_response(sent=sent, key=p["key"], mode=p.get("mode", "scancode"), focused=ok)


def _h_tap_key(p: dict) -> dict:
    ok, error = _focus_if_requested(_target_param(p), p.get("activate", True))
    if error:
        return error
    sent = win32.tap_key(p["key"], p.get("mode", "scancode"), p.get("hold_ms", 30))
    return ok_response(sent=sent, key=p["key"], mode=p.get("mode", "scancode"), focused=ok)


def _h_hotkey(p: dict) -> dict:
    ok, error = _focus_if_requested(_target_param(p), p.get("activate", True))
    if error:
        return error
    keys = p["keys"]
    mode = p.get("mode", "vk")
    sent = 0
    for key in keys:
        sent += win32.send_key_down(key, mode)
    for key in reversed(keys):
        sent += win32.send_key_up(key, mode)
    return ok_response(sent=sent, keys=keys, mode=mode, focused=ok)


def _h_type_text(p: dict) -> dict:
    ok, error = _focus_if_requested(_target_param(p), p.get("activate", True))
    if error:
        return error
    typed = win32.send_keys(p["text"])
    return ok_response(sent=typed, text=p["text"], focused=ok)
```

Register `key_down`, `key_up`, `tap_key`, `hotkey`, and `type_text` in `HANDLERS`.

In `server.py`, add matching MCP tools:

```python
@mcp.tool()
def key_down(target: dict[str, Any], key: str, mode: Literal["scancode", "vk"] = "scancode", activate: bool = True) -> dict:
    return _call("key_down", target=target, key=key, mode=mode, activate=activate)


@mcp.tool()
def key_up(target: dict[str, Any], key: str, mode: Literal["scancode", "vk"] = "scancode", activate: bool = True) -> dict:
    return _call("key_up", target=target, key=key, mode=mode, activate=activate)


@mcp.tool()
def tap_key(target: dict[str, Any], key: str, mode: Literal["scancode", "vk"] = "scancode", hold_ms: int = 30, activate: bool = True) -> dict:
    return _call("tap_key", target=target, key=key, mode=mode, hold_ms=hold_ms, activate=activate)


@mcp.tool()
def hotkey(target: dict[str, Any], keys: list[str], mode: Literal["scancode", "vk"] = "vk", activate: bool = True) -> dict:
    return _call("hotkey", target=target, keys=keys, mode=mode, activate=activate)


@mcp.tool()
def type_text(target: dict[str, Any], text: str, activate: bool = True) -> dict:
    return _call("type_text", target=target, text=text, activate=activate)
```

- [ ] **Step 6: Run key tests and daemon imports**

Run:

```powershell
uv run --extra dev pytest tests/test_keys.py -q
uv run --extra dev python -c "from windows_input_mcp.daemon import HANDLERS; print(all(name in HANDLERS for name in ['key_down','key_up','tap_key','hotkey','type_text']))"
```

Expected: first command PASS; second command prints `True`.

- [ ] **Step 7: Commit**

```powershell
git add windows_input_mcp/input windows_input_mcp/win32.py windows_input_mcp/daemon.py windows_input_mcp/server.py tests/test_keys.py
git commit -m "feat: add scan-code key controls"
```

---

### Task 11: Documentation And Full Verification

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml`
- Create: `tests/test_readme_contract.py`

**Interfaces:**
- Consumes: all public tools from Tasks 1-10
- Produces: updated user-facing docs for generic game capture and input

- [ ] **Step 1: Write failing README contract tests**

Create `tests/test_readme_contract.py`:

```python
from __future__ import annotations

from pathlib import Path


README = Path("README.md").read_text(encoding="utf-8")


def test_readme_describes_general_game_io_positioning() -> None:
    assert "general Windows game screenshot and input MCP" in README
    assert "Unity" not in README.split("## Tools", 1)[0]


def test_readme_documents_capture_and_frame_id_tools() -> None:
    assert "capture(target" in README
    assert "frame_id" in README
    assert 'scope="capture"' in README


def test_readme_documents_scan_code_keys() -> None:
    assert "key_down" in README
    assert "key_up" in README
    assert "scancode" in README
```

- [ ] **Step 2: Run the README tests to verify they fail**

Run:

```powershell
uv run --extra dev pytest tests/test_readme_contract.py -q
```

Expected: FAIL because README still describes Unity-first input.

- [ ] **Step 3: Update README**

Make these README changes:

```markdown
# game-input-mcp

General Windows game screenshot and input MCP. It captures target game windows
through OS/display capture backends and drives foreground games through an
elevated Win32 `SendInput` daemon.
```

Replace the tool list with:

```markdown
## Tools

- `list_targets()` - list visible candidate game windows.
- `get_target_info(target)` - resolve pid/hwnd and return window, client, DPI,
  monitor, and foreground metadata.
- `capture(target, region?, scope?, backend?, max_width?)` - capture target
  pixels and return `frame_id`, `image_path`, and geometry metadata.
- `focus_target(target)` / `focus_window(pid)` - bring the target foreground.
- `mouse_click(..., scope="capture", frame_id=...)`
- `mouse_drag(..., scope="capture", frame_id=...)`
- `scroll(..., scope="capture", frame_id=...)`
- `key_down(target, key, mode="scancode")`
- `key_up(target, key, mode="scancode")`
- `tap_key(target, key, mode="scancode")`
- `send_keys(pid, keys)` - compatibility text/key sequence helper.
```

Add a capture-to-click walkthrough:

````markdown
## Capture-To-Click Flow

1. Call `capture({"pid": <game-pid>})`.
2. Inspect the returned PNG at `image_path`.
3. Click an image pixel using the returned frame:

```python
mouse_click(
    pid=<game-pid>,
    x=640,
    y=360,
    scope="capture",
    frame_id="<frame_id>",
)
```
````

- [ ] **Step 4: Update package description**

In `pyproject.toml`, change:

```toml
description = "MCP server for Windows game screenshots and OS-level keyboard/mouse input with capture↔client↔screen coordinate translation."
```

- [ ] **Step 5: Run full unit tests**

Run:

```powershell
uv run --extra dev pytest -q
```

Expected: PASS.

- [ ] **Step 6: Run import smoke tests**

Run:

```powershell
uv run --extra dev python -m windows_input_mcp.server --help
uv run --extra dev python -c "from windows_input_mcp.capture import capture_region; from windows_input_mcp.input.keys import resolve_key; print(resolve_key('w'))"
```

Expected: commands exit successfully. The first command may start FastMCP help/banner behavior depending on `mcp`; if it blocks, stop it and use this non-blocking import check instead:

```powershell
uv run --extra dev python -c "import windows_input_mcp.server as s; print(s.mcp.name if hasattr(s.mcp, 'name') else 'server imported')"
```

- [ ] **Step 7: Commit**

```powershell
git add README.md pyproject.toml tests/test_readme_contract.py
git commit -m "docs: document general game io workflow"
```

---

## Final Verification

After all tasks are complete, run:

```powershell
uv run --extra dev pytest -q
uv run --extra dev python -c "from windows_input_mcp.daemon import HANDLERS; print(sorted(HANDLERS))"
uv run --extra dev python -c "from windows_input_mcp.capture import capture_region; print(capture_region)"
```

Expected:

- All tests pass.
- Handler list includes `capture`, `list_targets`, `get_target_info`, `focus_target`, `mouse_click`, `key_down`, `key_up`, `tap_key`, `hotkey`, and `type_text`.
- Capture import prints a function object.

Manual Windows smoke path:

```powershell
python -m windows_input_mcp.install --restart
notepad
```

Then in an MCP client:

1. `list_targets()`
2. `capture({"pid": <notepad-pid>})`
3. `mouse_click(pid=<notepad-pid>, x=20, y=20, scope="capture", frame_id="<frame_id>")`
4. `tap_key({"pid": <notepad-pid>}, "space")`

Expected:

- Capture returns a readable `image_path` and `frame_id`.
- Click lands in the captured Notepad client area.
- `tap_key` sends one key event pair.

## Self-Review Notes

- Spec coverage: target model is handled in Tasks 2-3; coordinate model in Task 4; file-backed frame cache in Task 5; capture backends in Tasks 6-7; daemon/server tools in Tasks 8-9; scan-code input in Task 10; docs and verification in Task 11.
- Deliberate v1 scope: `windows_graphics_capture` is deferred exactly as the approved implementation decision says.
- Compatibility: existing pid-based tools remain and gain optional `capture`/`frame_id` support.
- Security: no new broad system tools are introduced; existing named-pipe daemon model remains.
