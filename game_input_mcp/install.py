"""One-time installer / lifecycle helper for the game-input-daemon
scheduled task.

The daemon is registered as an ONLOGON task with /RL HIGHEST so it auto-
starts at every user login pre-elevated, with no UAC prompt at runtime —
schtasks handles the elevation grant once, at install time.

Usage:
    python -m game_input_mcp.install              # install + start now
    python -m game_input_mcp.install --uninstall  # remove
    python -m game_input_mcp.install --status     # check task state
    python -m game_input_mcp.install --restart    # stop + start now

Install/uninstall require an elevated shell — we just check IsUserAnAdmin()
and refuse with a clear message rather than trying to self-elevate, because
the relaunched process detaches from the console and the user can't see
schtasks output.
"""
from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
from pathlib import Path

TASK_NAME = "GameInputDaemon"


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _python_exe_for_task() -> Path:
    """Prefer pythonw.exe over python.exe so the daemon runs without a
    flashing console window. Both ship next to each other in standard
    Python installs and venvs.
    """
    p = Path(sys.executable)
    if p.name.lower() == "python.exe":
        pw = p.with_name("pythonw.exe")
        if pw.exists():
            return pw
    return p


def _schtasks(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["schtasks.exe", *args]
    print("$", " ".join(cmd))
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if cp.stdout:
        print(cp.stdout, end="")
    if cp.stderr:
        print(cp.stderr, end="", file=sys.stderr)
    if check and cp.returncode != 0:
        raise SystemExit(f"schtasks failed with exit {cp.returncode}")
    return cp


def install() -> None:
    if not _is_admin():
        raise SystemExit(
            "install requires elevation.\n"
            "Open PowerShell as Administrator and re-run:\n"
            f"  {sys.executable} -m game_input_mcp.install"
        )

    py = _python_exe_for_task()
    # schtasks /TR takes a single string; embed quotes around python path to
    # survive paths with spaces (e.g. Program Files).
    tr = f'"{py}" -m game_input_mcp.daemon'

    _schtasks(
        "/Create",
        "/TN", TASK_NAME,
        "/TR", tr,
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",
        "/F",
    )
    print(f"[ok] task '{TASK_NAME}' registered.")
    _schtasks("/Run", "/TN", TASK_NAME)
    print(f"[ok] task '{TASK_NAME}' started.")


def uninstall() -> None:
    if not _is_admin():
        raise SystemExit("uninstall requires elevation (see install message).")
    _schtasks("/End", "/TN", TASK_NAME, check=False)
    _schtasks("/Delete", "/TN", TASK_NAME, "/F", check=False)
    print(f"[ok] task '{TASK_NAME}' removed.")


def status() -> None:
    cp = _schtasks("/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST", check=False)
    if cp.returncode != 0:
        print(f"[info] task '{TASK_NAME}' not installed.")


def restart() -> None:
    if not _is_admin():
        raise SystemExit("restart requires elevation (see install message).")
    _schtasks("/End", "/TN", TASK_NAME, check=False)
    _schtasks("/Run", "/TN", TASK_NAME)
    print(f"[ok] task '{TASK_NAME}' restarted.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--uninstall", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--restart", action="store_true")
    args = ap.parse_args()

    if args.uninstall:
        uninstall()
    elif args.status:
        status()
    elif args.restart:
        restart()
    else:
        install()
    return 0


if __name__ == "__main__":
    sys.exit(main())
