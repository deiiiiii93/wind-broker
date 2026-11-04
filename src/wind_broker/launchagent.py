"""Install/uninstall ``~/Library/LaunchAgents/com.windbroker.daemon.plist``."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any

from .config import broker_home
from .transport import daemon_command

LABEL = "com.windbroker.daemon"


def plist_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def build_plist() -> dict[str, Any]:
    command, env = daemon_command()
    environment = {"WIND_BROKER_HOME": str(broker_home())}
    if "PYTHONPATH" in env:
        environment["PYTHONPATH"] = env["PYTHONPATH"].split(os.pathsep)[0]
        environment["PYTHONPYCACHEPREFIX"] = env["PYTHONPYCACHEPREFIX"]
    log = str(broker_home() / "windd.log")
    return {
        "Label": LABEL,
        "ProgramArguments": command,
        "EnvironmentVariables": environment,
        "WorkingDirectory": str(broker_home()),
        "RunAtLoad": True,
        # Restart after crashes, but not after a clean /v1/shutdown (exit 0).
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        # The Wind QR login window needs the user's GUI session.
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)  # noqa: S603,S607


def is_installed() -> bool:
    return plist_path().exists()


def is_loaded() -> bool:
    return _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0


def install(*, load: bool = True) -> dict[str, Any]:
    """Write the plist and load it unless launchd already has the job.

    Never boots out a loaded job: that would kill the running daemon and its
    Wind login. A rewritten plist takes effect at the next login.
    """

    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    broker_home().mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        plistlib.dump(build_plist(), handle)
    detail = ""
    ok = True
    if load:
        if is_loaded():
            detail = "already loaded; updated plist applies at next login"
        else:
            # If a windd is already running outside launchd, the launchd copy
            # sees the healthy daemon and exits 0, so it is not respawned.
            result = _launchctl("bootstrap", _domain(), str(path))
            ok, detail = result.returncode == 0, result.stderr.strip()
    return {"ok": ok, "path": str(path), "loaded": is_loaded() if load else False, "detail": detail}


def uninstall(*, stop: bool = False) -> dict[str, Any]:
    """Stop launching at login. The running daemon keeps its session unless ``stop``."""

    path = plist_path()
    path.unlink(missing_ok=True)
    if stop and is_loaded():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    return {"ok": True, "path": str(path), "loaded": is_loaded()}


def kickstart() -> bool:
    return _launchctl("kickstart", f"{_domain()}/{LABEL}").returncode == 0


def status() -> dict[str, Any]:
    return {"installed": is_installed(), "loaded": is_loaded(), "path": str(plist_path()), "label": LABEL}
