"""End-to-end tests: real HTTP daemon + real worker process + mock WindPy."""

from __future__ import annotations

import datetime as dt
import json
import threading
import urllib.error
import urllib.request

import pandas as pd
import pytest
from conftest import events, wait_for

from wind_broker import ProviderError, WindClient, w as default_w
from wind_broker.proxy import WindProxy

# Bypass any HTTP(S)_PROXY in the environment: windd is loopback-only.
open_url = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def test_status_before_login(daemon):
    status = daemon.client().status()
    assert status["state"] == "disconnected"
    assert status["connected"] is False
    assert status["sdk_installed"] is True
    assert status["daemon_pid"] > 0
    for key in ("uptime_seconds", "worker_pid", "connected_since", "last_active_at", "last_error",
                "total_requests", "failed_requests", "config"):
        assert key in status
    # The logged-out worker is spawned eagerly at boot.
    assert wait_for(lambda: daemon.client().status()["worker_pid"] is not None)


def test_login_once_across_many_clients_and_close_calls(daemon, home):
    for index in range(3):
        client = daemon.client(client_name=f"app-{index}")
        frame = client.call("wsd", "000300.SH", "close", "2024-01-02", "2024-01-03")
        assert frame["CLOSE"].tolist() == [3431.1, 3400.5]
        client.close()  # must be a no-op on the shared session
    proxy = WindProxy(daemon.client(client_name="notebook"))
    assert proxy.start().ErrorCode == 0
    proxy.stop()
    assert events(home).count("start") == 1
    assert "stop" not in events(home)
    status = daemon.client().status()
    assert status["connected"] and status["login_count"] == 1 and status["total_requests"] == 3


def test_concurrent_clients_share_one_login(daemon, home):
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            frame = daemon.client(client_name=f"thread-{index}").call("wsq", "000300.SH,000016.SH", "rt_last")
            assert frame.shape == (2, 3)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(32)]  # > old backlog of 5
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    assert events(home).count("start") == 1


def test_every_method_returns_exact_frames(daemon):
    client = daemon.client()
    wsd = client.call("wsd", "000300.SH", "close", dt.date(2024, 1, 2), dt.date(2024, 1, 3), "Fill=Blank")
    # date objects survive the round trip exactly (pandas' table orient alone would give strings).
    assert wsd.index.tolist() == [dt.date(2024, 1, 2), dt.date(2024, 1, 3)]
    wsq = client.call("wsq", "000300.SH,000016.SH,000905.SH,000852.SH", "rt_last,rt_date,rt_time")
    assert list(wsq.index) == ["000300.SH", "000016.SH", "000905.SH", "000852.SH"]
    assert wsq["RT_DATE"].dtype.kind == "M"
    wss = client.call("wss", "IF2403.CFE", "lasttrade_date")
    assert wss.iloc[0, 0] == pd.Timestamp("2024-03-15")
    wsi = client.call("wsi", "000300.SH", "close", "2024-01-02 09:30:00", "2024-01-02 15:00:00", "BarSize=15")
    assert isinstance(wsi.index, pd.DatetimeIndex) and wsi.shape == (3, 1)
    wset = client.call("wset", "optionchain", "date=2024-01-02;us_code=510050.SH")
    assert wset["wind_code"].tolist() == ["10006000.SH", "10006001.SH"]
    tdays = client.call("tdays", "2024-01-01", "2024-01-05", "")
    assert len(tdays) == 2


def test_table_orient_is_the_default_wire_format(daemon, home):
    token = (home / "token").read_text()
    request = urllib.request.Request(
        f"{daemon.url}/v1/call", method="POST", headers={"Content-Type": "application/json", "X-Wind-Client": "curl",
                                                       "Authorization": f"Bearer {token}"},
        data=json.dumps({"method": "wsq", "args": ["000300.SH", "rt_last"]}).encode())
    body = json.loads(open_url(request, timeout=10).read())
    assert body["ok"] and body["frame_format"] == "table" and body["rows"] == 1
    frame = pd.read_json(__import__("io").StringIO(body["frame"]), orient="table")
    assert frame.loc["000300.SH", "RT_LAST"] == 3500.0
    assert "latency_ms" in daemon.client().logs(1)[0]
    assert daemon.client().logs(1)[0]["client"] == "curl"


def test_vendor_error_raises_but_keeps_session(daemon, home):
    client = daemon.client()
    client.call("wsd", "000300.SH", "close")
    worker_pid = client.status()["worker_pid"]
    with pytest.raises(ProviderError, match="-40520007") as info:
        client.call("wsd", "ERR.SH", "badfield")
    assert info.value.kind == "vendor" and info.value.code == -40520007
    status = client.status()
    assert status["connected"] and status["worker_pid"] == worker_pid
    assert status["failed_requests"] == 1
    assert client.call("wsd", "000300.SH", "close").shape == (2, 1)
    assert events(home).count("start") == 1
    assert client.logs(5)[1]["status"] == "vendor_error"


def test_python_exception_in_sdk_keeps_session(daemon, home):
    client = daemon.client()
    client.call("wsq", "000300.SH", "rt_last")
    with pytest.raises(ProviderError, match="TypeError"):
        client.call("wsq", "PYERR", "rt_last")
    assert client.status()["connected"]
    assert events(home).count("start") == 1


def test_hung_call_times_out_and_recovers(daemon, home):
    client = daemon.client()
    client.call("wsd", "000300.SH", "close")
    old_worker = client.status()["worker_pid"]
    with pytest.raises(ProviderError, match="timed out") as info:
        client.call("wsd", "HANG.SH", "close", request_seconds=0.5)
    assert info.value.kind == "timeout"
    status = client.status()  # the HTTP daemon itself is still answering
    assert status["state"] == "error" and "timed out" in status["last_error"]
    assert status["worker_pid"] not in (None, old_worker) and status["worker_restarts"] == 1
    # The next request transparently logs in again on the fresh worker.
    assert client.call("wsd", "000300.SH", "close").shape == (2, 1)
    assert client.status()["connected"]
    assert events(home).count("start") == 2


def test_crashed_worker_is_replaced(daemon):
    client = daemon.client()
    client.call("wsq", "000300.SH", "rt_last")
    with pytest.raises(ProviderError, match="exited unexpectedly"):
        client.call("wsq", "CRASH", "rt_last")
    assert client.call("wsq", "000300.SH", "rt_last").shape == (1, 3)


def test_status_is_not_blocked_by_a_slow_call(daemon):
    client = daemon.client()
    client.call("wsq", "000300.SH", "rt_last")
    thread = threading.Thread(target=lambda: pytest.raises(ProviderError, client.call, "wsd", "HANG", "close",
                                                           request_seconds=1.5))
    thread.start()
    assert wait_for(lambda: client.logs(1) and True, 2)
    started = __import__("time").monotonic()
    client.status()
    assert __import__("time").monotonic() - started < 0.5
    thread.join(20)


def test_logout_calls_stop_and_resets(daemon, home):
    client = daemon.client()
    client.login()
    status = client.logout()
    assert status["state"] == "disconnected" and not status["connected"]
    assert events(home) == ["start", "stop"]
    client.call("wsq", "000300.SH", "rt_last")  # auto-connect again
    assert events(home).count("start") == 2


def test_async_login_reports_connecting(make_daemon, home, monkeypatch):
    monkeypatch.setenv("FAKE_WIND_LOGIN_DELAY", "1")
    running = make_daemon()
    client = running.client()
    status = client.login(wait=False)
    assert status["state"] == "connecting"
    assert wait_for(lambda: client.status()["connected"], 15)


def test_login_failure_is_reported(make_daemon, monkeypatch):
    monkeypatch.setenv("FAKE_WIND_LOGIN_FAIL", "1")
    running = make_daemon()
    client = running.client()
    with pytest.raises(ProviderError, match="login failed") as info:
        client.call("wsq", "000300.SH", "rt_last")
    assert info.value.kind == "login" and info.value.code == -40520004
    assert client.status()["state"] == "error"


def test_auto_connect_on_start(make_daemon, home):
    running = make_daemon(auto_connect_on_start=True)
    assert wait_for(lambda: running.client().status()["connected"], 15)
    assert events(home) == ["start"]


def test_history_cache_skips_sdk(make_daemon, home):
    running = make_daemon(history_cache_dir=str(home / "cache"))
    client = running.client()
    args = ("000300.SH", "close", "2020-01-02", "2020-01-03", "Fill=Blank")
    first = client.call("wsd", *args)
    second = client.call("wsd", *args)
    assert second.equals(first)
    assert client.logs(1)[0]["status"] == "cache"
    assert len(list((home / "cache").glob("*.json"))) == 1
    # A live (today) request is never cached.
    today = dt.date.today().isoformat()
    client.call("wsd", "000300.SH", "close", today, "2099-01-01")
    assert len(list((home / "cache").glob("*.json"))) == 1


def test_test_presets_and_custom(daemon):
    client = daemon.client()
    for preset in ("spot_live", "spot_daily", "tdays"):
        result = client.test(preset)
        assert result["ok"] and result["preview"]["row_count"] > 0
    live = client.test("spot_live")
    assert live["preview"]["columns"][1:] == ["RT_LAST", "RT_DATE", "RT_TIME"]
    custom = client.test("custom", method="wss", args=["IF2403.CFE", "lasttrade_date"])
    assert custom["preview"]["rows"][0][0] == "IF2403.CFE"


def test_config_roundtrip(daemon, home):
    client = daemon.client()
    result = client.set_config(request_seconds=30, port=19999)
    assert result["restart_required"] == ["port"]
    assert client.status()["config"]["request_seconds"] == 30
    saved = json.loads((home / "config.json").read_text())
    assert saved["port"] == 19999 and saved["request_seconds"] == 30
    with pytest.raises(ProviderError, match="unknown config"):
        client.set_config(bogus=1)
    with pytest.raises(ProviderError, match="positive"):
        client.set_config(request_seconds=-1)


def test_logs_ring_buffer(daemon):
    client = daemon.client(client_name="ring")
    for _ in range(3):
        client.call("wsq", "000300.SH", "rt_last")
    entries = client.logs(2)
    assert len(entries) == 2 and entries[0]["id"] > entries[1]["id"]
    assert {"timestamp", "client", "method", "args_summary", "rows", "cols", "latency_ms", "status",
            "detail"} <= set(entries[0])
    client._request("POST", "/v1/logs/clear")
    assert client.logs() == []


def test_unix_socket_transport(daemon, home):
    client = WindClient(socket_path=str(home / "wind.sock"), base_url="http://127.0.0.1:1",
                        autospawn=False, startup_seconds=5, request_seconds=2)
    assert client.call("wsq", "000300.SH", "rt_last").shape == (1, 3)


def test_rejects_browser_style_requests(daemon, home):
    # Even a caller holding the token must use JSON and a loopback Host header.
    auth = {"Authorization": f"Bearer {(home / 'token').read_text()}"}
    request = urllib.request.Request(f"{daemon.url}/v1/shutdown", method="POST", data=b"{}",
                                     headers={"Content-Type": "text/plain", **auth})
    with pytest.raises(urllib.error.HTTPError) as info:
        open_url(request, timeout=5)
    assert info.value.code == 415
    request = urllib.request.Request(f"{daemon.url}/v1/status", headers={"Host": "evil.example:18881", **auth})
    with pytest.raises(urllib.error.HTTPError) as info:
        open_url(request, timeout=5)
    assert info.value.code == 403


def test_bad_requests(daemon):
    client = daemon.client()
    with pytest.raises(ValueError):
        client.call("w_order", "x")
    with pytest.raises(ProviderError, match="Unsupported"):
        client.call_raw("w_order", [])
    with pytest.raises(ProviderError, match="(?i)callback"):
        client.call("wsq", "000300.SH", "rt_last", func="x")


def test_w_proxy_mimics_windpy(daemon):
    w = WindProxy(daemon.client(client_name="notebook"))
    assert w.start(waitTime=5).ErrorCode == 0
    assert w.isconnected()
    data = w.wsd("000300.SH", "close", "2024-01-02", "2024-01-03")
    assert data.ErrorCode == 0 and data.Fields == ["CLOSE"] and data.Times[0] == dt.date(2024, 1, 2)
    error, frame = w.wsq("000300.SH", "rt_last", usedf=True)
    assert error == 0 and frame.shape == (1, 3)
    # Vendor errors are returned, not raised, exactly like WindPy.
    bad = w.wsd("ERR.SH", "x")
    assert bad.ErrorCode == -40520007
    error, _ = w.wsd("ERR.SH", "x", usedf=True)
    assert error == -40520007
    w.stop()
    assert w.isconnected()
    assert "wsd" in dir(default_w)


def test_tcp_requires_owner_token_but_unix_socket_does_not(daemon, home):
    import http.client
    import os
    import stat

    from wind_broker.transport import UnixHTTPConnection

    token_file = home / "token"
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    # Another local account can reach loopback TCP but cannot read the token.
    for headers in ({}, {"Authorization": "Bearer wrong-token"}):
        request = urllib.request.Request(f"{daemon.url}/v1/status", headers=headers)
        with pytest.raises(urllib.error.HTTPError) as info:
            open_url(request, timeout=5)
        assert info.value.code == 401
    hostile = urllib.request.Request(
        f"{daemon.url}/v1/config", method="POST", data=json.dumps({"wind_module_path": "/tmp/evil.py"}).encode(),
        headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as info:
        open_url(hostile, timeout=5)
    assert info.value.code == 401
    assert json.loads((home / "config.json").read_text())["wind_module_path"].endswith("fake_windpy.py")
    # With the token it works; the 0600 Unix socket needs none.
    ok = urllib.request.Request(f"{daemon.url}/v1/status",
                                headers={"Authorization": f"Bearer {token_file.read_text()}"})
    assert json.loads(open_url(ok, timeout=5).read())["ok"]
    connection = UnixHTTPConnection(str(home / "wind.sock"), timeout=5)
    connection.request("GET", "/v1/status")
    assert connection.getresponse().status == 200
    connection.close()


def test_api_cannot_expose_daemon_to_network(daemon):
    with pytest.raises(ProviderError, match="non-loopback"):
        daemon.client().set_config(host="0.0.0.0")
