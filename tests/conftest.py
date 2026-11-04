from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

FAKE = Path(__file__).with_name("fake_windpy.py")


@pytest.fixture
def home(monkeypatch):
    # AF_UNIX paths are limited to 104 bytes on macOS; pytest's tmp_path is too long.
    directory = Path(tempfile.mkdtemp(prefix="wb-", dir="/tmp"))
    monkeypatch.setenv("WIND_BROKER_HOME", str(directory))
    monkeypatch.setenv("FAKE_WIND_EVENTS", str(directory / "events.log"))
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")  # never let ~/.local WindPy leak into tests
    monkeypatch.delenv("WIND_BROKER_URL", raising=False)
    monkeypatch.delenv("WIND_BROKER_SOCKET", raising=False)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def write_config(home: Path, **overrides) -> Path:
    config = {
        "host": "127.0.0.1", "port": 0, "unix_socket_path": str(home / "wind.sock"),
        "wind_module_path": str(FAKE), "startup_seconds": 5, "request_seconds": 2.0,
        "auto_connect_on_start": False, **overrides,
    }
    path = home / "config.json"
    path.write_text(json.dumps(config))
    return path


def events(home: Path) -> list[str]:
    path = home / "events.log"
    return [line.split()[0] for line in path.read_text().splitlines()] if path.exists() else []


class RunningDaemon:
    def __init__(self, daemon, thread):
        self.daemon, self.thread = daemon, thread

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.daemon.config.port}"

    @property
    def broker(self):
        return self.daemon.broker

    def client(self, **kwargs):
        from wind_broker import WindClient

        kwargs.setdefault("autospawn", False)
        kwargs.setdefault("startup_seconds", 5)
        kwargs.setdefault("request_seconds", 2)
        return WindClient(base_url=self.url, **kwargs)


@pytest.fixture
def make_daemon(home):
    from wind_broker.daemon import Daemon

    started: list[RunningDaemon] = []

    def factory(**overrides) -> RunningDaemon:
        daemon = Daemon(write_config(home, **overrides))
        thread = threading.Thread(target=daemon.serve, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not daemon._servers or daemon.config.port == 0:
            assert time.monotonic() < deadline, "daemon did not bind"
            time.sleep(0.02)
        running = RunningDaemon(daemon, thread)
        started.append(running)
        return running

    yield factory
    for running in started:
        running.daemon.stop()
        running.thread.join(10)


@pytest.fixture
def daemon(make_daemon):
    return make_daemon()


def wait_for(predicate, seconds: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture(autouse=True)
def _no_stray_env(monkeypatch):
    for name in ("FAKE_WIND_LOGIN_FAIL", "FAKE_WIND_LOGIN_DELAY"):
        monkeypatch.delenv(name, raising=False)
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
