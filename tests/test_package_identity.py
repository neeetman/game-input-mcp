from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackageIdentityTests(unittest.TestCase):
    def test_imports_game_input_package_and_uses_game_pipe(self) -> None:
        module = importlib.import_module("game_input_mcp")
        ipc = importlib.import_module("game_input_mcp.ipc")

        self.assertIn("game-input-mcp", module.__doc__ or "")
        self.assertEqual(r"\\.\pipe\game-input-mcp", ipc.PIPE_PREFIX)

    def test_pyproject_uses_game_input_distribution_and_scripts(self) -> None:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual("game-input-mcp", data["project"]["name"])
        self.assertEqual(
            {
                "game-input-mcp": "game_input_mcp.server:main",
                "game-input-daemon": "game_input_mcp.daemon:main",
                "game-input-install": "game_input_mcp.install:main",
            },
            data["project"]["scripts"],
        )
        self.assertEqual(["game_input_mcp"], data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])


if __name__ == "__main__":
    unittest.main()
