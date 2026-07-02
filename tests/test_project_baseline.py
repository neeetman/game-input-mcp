from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


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
