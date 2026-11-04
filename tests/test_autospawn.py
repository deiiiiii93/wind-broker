"""The client starts a detached windd that outlives the calling process."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from conftest import events, wait_for, write_config

from wind_broker import WindClient
from wind_broker.transport import Transport

SRC = str(Path(__file__).resolve().parent.parent / "src")


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_client_autospawns_detached_daemon_that_survives_caller(home):
    port = _free_port()
    write_config(home, port=port, python_executable=sys.executable)
    script = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {SRC!r})
        from wind_broker import WindClient
        client = WindClient(startup_seconds=5, request_seconds=5, client_name="short-lived")
        print(client.call("wsd", "000300.SH", "close").shape)
        client.close()
    """)
    env = {**os.environ, "PYTHONPATH": ""}
    pid = None
    try:
        # Two short-lived processes in a row: only the first spawns windd,
        # and only one Wind login ever happens.
        for _ in range(2):
            out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)
            assert out.returncode == 0, out.stderr
            assert "(2, 1)" in out.stdout
        transport = Transport()
        assert transport.healthy()
        status = WindClient(autospawn=False).status()
        pid = status["daemon_pid"]
        assert os.getpgid(pid) == pid  # own session/process group: detached from callers
        assert status["login_count"] == 1
        assert events(home).count("start") == 1
    finally:
        if pid is None:
            pid_file = home / "windd.pid"
            pid = int(pid_file.read_text()) if pid_file.exists() else None
        if pid:
            os.kill(pid, signal.SIGTERM)
            assert wait_for(lambda: not Transport().healthy(timeout=0.3), 15)
            time.sleep(0.2)
    # SIGTERM is a daemon shutdown: the session is stopped cleanly.
    assert events(home)[-1] == "stop"
