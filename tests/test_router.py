"""LAN router (hub mode): config, workstation key registry, LAN listener, SDK remote mode."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request

import pytest
from conftest import events, wait_for

from wind_broker import ProviderError
from wind_broker.codec import decode_frame
from wind_broker.config import BrokerConfig, load_config


# ----------------------------------------------------------------- config
def test_router_config_defaults_off(home):
    config = load_config(home / "missing.json")
    assert config.router_enabled is False
    assert config.router_host == "0.0.0.0"
    assert config.router_port == 18882
    assert config.router_max_inflight == 2
    assert not {"router_enabled", "router_host", "router_port", "router_max_inflight"} & BrokerConfig.RESTART_KEYS


def test_old_config_without_router_keys_loads(home):
    path = home / "config.json"
    path.write_text(json.dumps({"host": "127.0.0.1", "port": 18881}))
    config = load_config(path)
    assert config.router_enabled is False and config.router_port == 18882


@pytest.mark.parametrize("changes", [
    {"router_enabled": "yes"},
    {"router_host": ""},
    {"router_port": 70000},
    {"router_port": True},
    {"router_max_inflight": 0},
    {"router_max_inflight": 1.5},
    {"port": 18881, "router_port": 18881},
])
def test_router_config_validation(changes):
    with pytest.raises(ValueError):
        BrokerConfig().updated(changes)


def test_router_port_may_equal_port_when_both_ephemeral():
    assert BrokerConfig().updated({"port": 0, "router_port": 0}).router_port == 0


# --------------------------------------------------------------- registry
def test_registry_add_verify_revoke_and_persist(home):
    from wind_broker.router import ClientRegistry, registry_path

    registry = ClientRegistry()
    key = registry.add("alice-pc")
    assert key.startswith("wbk_") and len(key) > 30
    assert registry.verify(key) == "alice-pc"
    assert registry.verify("wbk_wrong") is None
    assert registry.verify("") is None

    path = registry_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    raw = path.read_text()
    assert key not in raw  # only the digest is stored
    entry = json.loads(raw)["clients"][0]
    assert entry["name"] == "alice-pc" and len(entry["key_sha256"]) == 64 and entry["created_at"]

    reloaded = ClientRegistry()  # survives a daemon restart
    assert reloaded.verify(key) == "alice-pc"
    reloaded.revoke("alice-pc")
    assert reloaded.verify(key) is None
    assert ClientRegistry().verify(key) is None


@pytest.mark.parametrize("name", ["", "-lead", "has space", "x" * 41, "a/b", "über", "jack\n"])
def test_registry_rejects_bad_names(home, name):
    from wind_broker.router import ClientRegistry

    with pytest.raises(ValueError):
        ClientRegistry().add(name)


def test_registry_rejects_duplicates_and_unknown_revoke(home):
    from wind_broker.router import ClientRegistry

    registry = ClientRegistry()
    registry.add("bob")
    with pytest.raises(ValueError):
        registry.add("bob")
    with pytest.raises(KeyError):
        registry.revoke("nobody")


def test_registry_corrupt_file_counts_as_empty(home):
    from wind_broker.router import ClientRegistry, registry_path

    registry_path().write_text("{not json")
    registry = ClientRegistry()
    assert registry.error and registry.clients() == []
    assert registry_path().read_text() == "{not json"  # untouched until an add/revoke
    key = registry.add("carol")
    assert ClientRegistry().verify(key) == "carol"
    assert registry.error is None


def test_registry_inflight_cap_and_stats(home):
    from wind_broker.router import ClientRegistry

    registry = ClientRegistry()
    registry.add("dave")
    assert registry.acquire("dave", 2) and registry.acquire("dave", 2)
    assert not registry.acquire("dave", 2)
    registry.release("dave")
    assert registry.acquire("dave", 2)
    registry.touch("dave", "10.0.0.5")
    (info,) = registry.clients()
    assert info["name"] == "dave" and info["inflight"] == 2 and info["requests"] == 1
    assert info["last_addr"] == "10.0.0.5" and info["last_seen_at"]
    assert "key_sha256" not in info


# ----------------------------------------------------------- LAN listener
open_url = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
WSD_ARGS = ["000300.SH", "close", "2024-01-02", "2024-01-03"]


def http(url: str, path: str, *, key: str | None = None, body: dict | None = None,
         method: str | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url + path, data=data, method=method or ("POST" if data is not None else "GET"))
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with open_url(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def router_url(running) -> str:
    return f"http://127.0.0.1:{running.daemon.router.port}"


def add_client(running, name: str) -> str:
    return running.client()._request("POST", "/v1/router/clients", {"name": name})["key"]


def router_settled(running) -> bool:
    return wait_for(lambda: running.daemon.router.listening or running.daemon.router.error is not None)


@pytest.fixture
def hub(make_daemon):
    def factory(**overrides):
        settings = {"router_enabled": True, "router_host": "127.0.0.1", "router_port": 0, **overrides}
        running = make_daemon(**settings)
        assert router_settled(running)
        return running

    return factory


def test_router_off_by_default(daemon):
    status = daemon.client().status()
    assert status["router"] == {"enabled": False, "listening": False, "port": 18882, "clients": 0}
    assert not daemon.daemon.router.listening


def test_remote_call_returns_frames_and_is_attributed(hub):
    running = hub()
    key = add_client(running, "alice-pc")
    running.client().login()
    status, body = http(router_url(running), "/v1/call", key=key,
                        body={"method": "wsd", "args": WSD_ARGS, "frame_format": "exact", "client": "notebook"})
    assert status == 200, body
    assert decode_frame(body)["CLOSE"].tolist() == [3431.1, 3400.5]
    status, body = http(router_url(running), "/v1/call", key=key,
                        body={"method": "wsd", "args": WSD_ARGS, "usedf": False})
    assert status == 200 and body["windata"]["ErrorCode"] == 0
    clients = [entry["client"] for entry in running.client().logs(10)]
    assert "alice-pc/notebook" in clients and "alice-pc/remote" in clients


def test_router_status_is_trimmed(hub):
    running = hub()
    key = add_client(running, "bob")
    status, body = http(router_url(running), "/v1/status", key=key)
    assert status == 200
    assert body["client"] == "bob" and body["router"] is True and body["connected"] is False
    for secret in ("daemon_pid", "worker_pid", "config", "last_error", "sdk_installed"):
        assert secret not in body
    assert http(router_url(running), "/v1/health", key=key)[0] == 200


def test_router_rejects_missing_wrong_local_and_revoked_keys(hub, home):
    running = hub()
    key = add_client(running, "carol")
    local_token = (home / "token").read_text().strip()
    for bad in (None, "wbk_wrong", local_token):
        status, body = http(router_url(running), "/v1/status", key=bad)
        assert status == 401 and body["error"]["kind"] == "unauthorized"
    status, _ = http(router_url(running), "/v1/status", headers={"X-Wind-Token": key})
    assert status == 200
    running.client()._request("POST", "/v1/router/clients/revoke", {"name": "carol"})
    assert http(router_url(running), "/v1/status", key=key)[0] == 401


def test_router_forbids_admin_routes(hub, home):
    running = hub()
    key = add_client(running, "dave")
    url = router_url(running)
    config_before = (home / "config.json").read_text()
    for path in ("/v1/config", "/v1/logs", "/v1/router"):
        status, body = http(url, path, key=key)
        assert status == 403 and body["error"]["kind"] == "forbidden", path
    for path, payload in (("/v1/login", {}), ("/v1/logout", {}), ("/v1/config", {"request_seconds": 9}),
                          ("/v1/test", {"preset": "tdays"}), ("/v1/shutdown", {}), ("/v1/logs/clear", {}),
                          ("/v1/router/clients", {"name": "evil"})):
        status, body = http(url, path, key=key, body=payload)
        assert status == 403 and body["error"]["kind"] == "forbidden", path
    status = running.client().status()
    assert status["login_count"] == 0 and status["router"]["clients"] == 1
    assert (home / "config.json").read_text() == config_before
    assert running.daemon.router.listening


def test_router_requires_json_content_type(hub):
    running = hub()
    key = add_client(running, "erin")
    request = urllib.request.Request(router_url(running) + "/v1/call", data=b"{}", method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        open_url(request, timeout=10)
    assert caught.value.code == 415


def test_router_caps_inflight_per_workstation(hub):
    running = hub(router_max_inflight=1)
    slow_key, other_key = add_client(running, "slow"), add_client(running, "other")
    running.client().login()
    url = router_url(running)
    results: dict[str, tuple[int, dict]] = {}

    def remote(name: str, key: str, codes: str) -> None:
        results[name] = http(url, "/v1/call", key=key, body={"method": "wsd", "args": [codes, *WSD_ARGS[1:]]})

    first = threading.Thread(target=remote, args=("first", slow_key, "SLOW.SH"))
    first.start()
    assert wait_for(lambda: running.daemon.router.registry.clients()[0]["inflight"] == 1)
    status, body = http(url, "/v1/call", key=slow_key, body={"method": "wsd", "args": WSD_ARGS})
    assert status == 429 and body["error"]["kind"] == "busy"
    other = threading.Thread(target=remote, args=("other", other_key, "000300.SH"))
    other.start()
    local = running.client(client_name="local-app").call("wsd", *WSD_ARGS)  # local clients are never capped
    assert local.shape == (2, 1)
    first.join(30)
    other.join(30)
    assert results["first"][0] == 200 and results["other"][0] == 200
    assert wait_for(lambda: all(c["inflight"] == 0 for c in running.daemon.router.registry.clients()))


def test_remote_call_never_logs_in(hub, home):
    running = hub()
    key = add_client(running, "frank")
    status, body = http(router_url(running), "/v1/call", key=key, body={"method": "wsd", "args": WSD_ARGS})
    assert status == 503 and body["error"]["kind"] == "not_connected"
    assert events(home).count("start") == 0
    assert running.client().status()["login_count"] == 0


def test_remote_history_cache_hit_works_while_hub_logged_out(hub, home):
    running = hub(history_cache_dir=str(home / "cache"))
    key = add_client(running, "grace")
    running.client().call("wsd", *WSD_ARGS)  # fills the history cache
    running.client().logout()
    status, body = http(router_url(running), "/v1/call", key=key, body={"method": "wsd", "args": WSD_ARGS})
    assert status == 200 and body["source"] == "cache"
    assert events(home).count("start") == 1


def test_remote_request_seconds_is_clamped(hub):
    running = hub()
    key = add_client(running, "henry")
    running.client().login()
    started = time.monotonic()
    status, body = http(router_url(running), "/v1/call", key=key,
                        body={"method": "wsd", "args": ["HANG.SH", *WSD_ARGS[1:]], "request_seconds": 600})
    assert status == 504 and body["error"]["kind"] == "timeout"
    assert time.monotonic() - started < 20  # hub request_seconds (2s) applied, not 600


def test_router_toggles_live_without_touching_session(daemon):
    client = daemon.client()
    client.login()
    before = client.status()
    response = client.set_config(router_enabled=True, router_host="127.0.0.1", router_port=0)
    assert response["router"]["listening"] is True and response["restart_required"] == []
    port = daemon.daemon.router.port
    key = add_client(daemon, "ivy")
    assert http(router_url(daemon), "/v1/status", key=key)[0] == 200
    client.set_config(router_enabled=True, router_host="127.0.0.1", router_port=0)
    assert daemon.daemon.router.port == port  # unchanged keys: no rebind
    assert client.set_config(request_seconds=3.0)["router"]["port"] == port
    response = client.set_config(router_enabled=False)
    assert response["router"]["listening"] is False
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    after = client.status()
    assert after["login_count"] == before["login_count"] == 1
    assert after["worker_pid"] == before["worker_pid"] and after["connected"]
    saved = json.loads(daemon.daemon.config_file.read_text())
    assert saved["router_port"] == 0  # the resolved port is never persisted


def test_disabling_router_cuts_open_keepalive_connections(hub):
    import http.client

    running = hub()
    key = add_client(running, "olga")
    connection = http.client.HTTPConnection("127.0.0.1", running.daemon.router.port, timeout=10)
    headers = {"Authorization": f"Bearer {key}"}
    try:
        connection.request("GET", "/v1/status", headers=headers)
        response = connection.getresponse()
        response.read()
        assert response.status == 200 and not response.will_close
        running.client().set_config(router_enabled=False)
        try:
            connection.request("GET", "/v1/status", headers=headers)
            response = connection.getresponse()
            response.read()
            served = response.status
        except (OSError, http.client.HTTPException):
            served = None  # connection was cut: the expected outcome
        assert served != 200
    finally:
        connection.close()


def test_rejected_post_closes_the_connection(hub):
    import http.client

    running = hub()
    connection = http.client.HTTPConnection("127.0.0.1", running.daemon.router.port, timeout=10)
    try:
        connection.request("POST", "/v1/call", body=b'{"method": "wsd"}',
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        assert response.status == 401 and response.will_close  # unread body never parsed as a request
    finally:
        connection.close()


def test_router_port_in_use_is_not_fatal(make_daemon):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        running = make_daemon(router_enabled=True, router_host="127.0.0.1", router_port=busy_port)
        assert router_settled(running)
        status = running.client().status()  # the local API still serves
        assert status["router"]["listening"] is False
        info = running.client()._request("GET", "/v1/router")
        assert info["error"] and str(busy_port) in info["error"]

        response = running.client().set_config(router_port=0)
        assert response["router"]["listening"] is True and response["router"]["error"] is None
        response = running.client().set_config(router_port=busy_port)
        assert response["ok"] and response["router"]["listening"] is False and response["router"]["error"]
        assert json.loads(running.daemon.config_file.read_text())["router_port"] == busy_port
    finally:
        blocker.close()


def test_router_port_must_differ_from_port(daemon):
    with pytest.raises(ProviderError) as caught:
        daemon.client().set_config(router_port=daemon.daemon.config.port)
    assert caught.value.kind == "bad_request"


def test_corrupt_registry_does_not_stop_daemon(make_daemon, home):
    (home / "router_clients.json").write_text("[broken")
    running = make_daemon(router_enabled=True, router_host="127.0.0.1", router_port=0)
    assert router_settled(running)
    info = running.client()._request("GET", "/v1/router")
    assert info["clients"] == [] and info["error"] and info["listening"] is True


def test_router_management_endpoints(hub):
    running = hub()
    client = running.client()
    key = add_client(running, "jack")
    info = client._request("GET", "/v1/router")
    assert info["enabled"] is True and info["host"] == "127.0.0.1" and info["port"] == running.daemon.router.port
    assert info["max_inflight"] == 2 and isinstance(info["addresses"], list)
    assert [c["name"] for c in info["clients"]] == ["jack"] and key not in json.dumps(info)
    for bad in ({"name": "jack"}, {"name": "bad name"}, {}):
        with pytest.raises(ProviderError) as caught:
            client._request("POST", "/v1/router/clients", bad)
        assert caught.value.kind == "bad_request"
    status, body = http(running.url, "/v1/router/clients/revoke", body={"name": "nobody"}, key=running.daemon.token)
    assert status == 404 and body["error"]["kind"] == "not_found"


def test_router_closes_idle_unauthenticated_connections(hub, monkeypatch):
    import wind_broker.router as router_module

    monkeypatch.setattr(router_module, "ROUTER_SOCKET_TIMEOUT", 0.5)
    running = hub()
    connection = socket.create_connection(("127.0.0.1", running.daemon.router.port), timeout=10)
    try:
        started = time.monotonic()
        assert connection.recv(1) == b""  # server hung up
        assert time.monotonic() - started < 8
    finally:
        connection.close()


# ------------------------------------------------------- SDK remote mode
def remote_client(running, key: str, **kwargs):
    from wind_broker import WindClient

    return WindClient(base_url=router_url(running), api_key=key, startup_seconds=5, request_seconds=2, **kwargs)


def test_sdk_remote_mode_against_router(hub, monkeypatch):
    import wind_broker.client as client_module
    import wind_broker.transport as transport_module
    from wind_broker.proxy import WindProxy

    running = hub()
    key = add_client(running, "lena")
    running.client().login()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("remote mode must not touch the local token or spawn windd")

    monkeypatch.setattr(transport_module, "read_token", forbidden)
    monkeypatch.setattr(client_module, "spawn_daemon", forbidden)
    client = remote_client(running, key, client_name="research")
    assert client.remote and client.autospawn is False
    frame = client.call("wsd", *WSD_ARGS)
    assert frame["CLOSE"].tolist() == [3431.1, 3400.5]
    assert client.windata("wsd", *WSD_ARGS)["ErrorCode"] == 0
    proxy = WindProxy(client)
    assert proxy.start().ErrorCode == 0 and proxy.isconnected()
    assert proxy.wsd(*WSD_ARGS).ErrorCode == 0
    error, df = proxy.wsd(*WSD_ARGS, usedf=True)
    assert error == 0 and df.shape == (2, 1)
    with pytest.raises(ProviderError) as caught:
        client.logout()  # admin routes stay local
    assert caught.value.kind == "forbidden"
    # Local API calls would read the (patched) token: inspect the broker directly.
    assert "lena/research" in [e["client"] for e in running.broker.recent_logs(20)]
    assert running.broker.status()["login_count"] == 1


def test_sdk_remote_mode_from_environment(hub, monkeypatch):
    from wind_broker import WindClient

    running = hub()
    key = add_client(running, "mike")
    running.client().login()
    monkeypatch.setenv("WIND_BROKER_URL", router_url(running))
    monkeypatch.setenv("WIND_BROKER_KEY", key)
    client = WindClient(startup_seconds=5, request_seconds=2)
    assert client.remote
    assert client.call("wsd", *WSD_ARGS).shape == (2, 1)


def test_sdk_remote_start_reports_hub_logged_out(hub, home):
    from wind_broker.proxy import WindProxy

    running = hub()
    key = add_client(running, "nina")
    result = WindProxy(remote_client(running, key)).start()
    assert result.ErrorCode != 0 and "not logged in" in str(result.Data)
    assert events(home).count("start") == 0


def test_sdk_remote_mode_never_autospawns(home, monkeypatch):
    import wind_broker.client as client_module
    from wind_broker import DaemonUnavailable, WindClient

    monkeypatch.setattr(client_module, "spawn_daemon", lambda **_: pytest.fail("autospawn in remote mode"))
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    client = WindClient(base_url=f"http://127.0.0.1:{port}", api_key="wbk_x", autospawn=True)
    with pytest.raises(DaemonUnavailable):
        client.status()


def test_sdk_key_without_url_is_a_configuration_error(home, monkeypatch):
    from wind_broker import WindClient

    with pytest.raises(ValueError, match="hub URL"):
        WindClient(api_key="wbk_x")
    monkeypatch.setenv("WIND_BROKER_KEY", "wbk_x")
    with pytest.raises(ValueError, match="hub URL"):
        WindClient()


def test_local_token_is_only_sent_to_local_targets(home):
    from wind_broker.config import ensure_token
    from wind_broker.transport import Transport

    token = ensure_token()
    assert Transport(base_url="http://192.168.1.50:18882")._auth_token(unix=False) is None
    assert Transport(base_url="http://hub-mac.local:18882")._auth_token(unix=False) is None
    assert Transport(base_url="http://127.0.0.1:18881")._auth_token(unix=False) == token
    assert Transport(base_url="http://localhost:18881")._auth_token(unix=False) == token
    assert Transport(base_url="http://192.168.1.50:18882")._auth_token(unix=True) == token
    assert Transport(base_url="http://192.168.1.50:18882", api_key="wbk_k")._auth_token(unix=False) == "wbk_k"


def test_daemon_command_drops_remote_settings(home, monkeypatch):
    from wind_broker.transport import daemon_command

    monkeypatch.setenv("WIND_BROKER_KEY", "wbk_x")
    monkeypatch.setenv("WIND_BROKER_URL", "http://hub:18882")
    _, env = daemon_command()
    assert "WIND_BROKER_KEY" not in env and "WIND_BROKER_URL" not in env


def test_sdk_imports_without_fcntl():
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src"
    code = ("import sys; sys.modules['fcntl'] = None\n"
            "import wind_broker, wind_broker_client\n"
            "from wind_broker import WindClient, w\n"
            "WindClient(base_url='http://192.168.1.50:18882', api_key='wbk_x')\n")
    result = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(src)},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------------- CLI
def test_windctl_router_commands(daemon, capsys, monkeypatch):
    import re

    from wind_broker import cli

    # A workstation-style environment must not redirect hub management commands.
    monkeypatch.setenv("WIND_BROKER_KEY", "wbk_elsewhere")
    monkeypatch.setenv("WIND_BROKER_URL", "http://192.0.2.1:18882")

    assert cli.main(["router", "enable", "--host", "127.0.0.1", "--port", "0", "--max-inflight", "3"]) == 0
    captured = capsys.readouterr()
    assert "Wind agreement" in captured.err
    assert daemon.daemon.router.listening and daemon.daemon.config.router_max_inflight == 3

    assert cli.main(["router", "add-client", "paul"]) == 0
    out = capsys.readouterr().out
    key = re.search(r"wbk_[A-Za-z0-9_-]+", out).group(0)
    assert f"WIND_BROKER_KEY={key}" in out and "WIND_BROKER_URL=http://" in out
    monkeypatch.delenv("WIND_BROKER_KEY", raising=False)
    monkeypatch.delenv("WIND_BROKER_URL", raising=False)
    assert http(router_url(daemon), "/v1/status", key=key)[0] == 200
    monkeypatch.setenv("WIND_BROKER_KEY", "wbk_elsewhere")

    assert cli.main(["--json", "router", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["listening"] is True and [c["name"] for c in status["clients"]] == ["paul"]

    assert cli.main(["router", "revoke", "paul"]) == 0
    assert cli.main(["router", "revoke", "nobody"]) == 2
    capsys.readouterr()
    monkeypatch.delenv("WIND_BROKER_KEY", raising=False)
    assert http(router_url(daemon), "/v1/status", key=key)[0] == 401

    assert cli.main(["router", "disable"]) == 0
    assert not daemon.daemon.router.listening


def test_router_keys_survive_daemon_restart(make_daemon):
    first = make_daemon(router_enabled=True, router_host="127.0.0.1", router_port=0)
    assert router_settled(first)
    key = add_client(first, "kate")
    first.daemon.stop()
    first.thread.join(10)
    second = make_daemon(router_enabled=True, router_host="127.0.0.1", router_port=0)
    assert router_settled(second)
    assert http(router_url(second), "/v1/status", key=key)[0] == 200
