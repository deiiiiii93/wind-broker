"""``windctl``: control the WindBroker daemon from the terminal (and from WindBroker.app)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from . import launchagent
from .client import WindClient
from .codec import preview
from .errors import DaemonUnavailable, ProviderError
from .transport import Transport, spawn_daemon


def _print(data: Any, as_json: bool) -> None:
    if as_json or not isinstance(data, dict):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, default=str)
        print(f"{key:>18}: {value}")


def _table(table: dict[str, Any]) -> None:
    columns, rows = table["columns"], table["rows"]
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c))
              for i, c in enumerate(columns)]
    print("  ".join(str(c).ljust(w) for c, w in zip(columns, widths)))
    for row in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths)))
    print(f"({table['row_count']} rows x {table['col_count']} cols{', truncated' if table.get('truncated') else ''})")


def _parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="windctl", description="Control the local WindBroker daemon")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="daemon + session status")
    sub.add_parser("start", help="start windd detached (no-op if running)")
    sub.add_parser("stop", help="stop windd (calls w.stop())")
    sub.add_parser("restart", help="stop then start windd")
    login = sub.add_parser("login", help="log in to Wind (QR window if needed)")
    login.add_argument("--no-wait", action="store_true", help="return immediately; poll status")
    sub.add_parser("logout", help="w.stop() the shared session")
    logs = sub.add_parser("logs", help="recent requests")
    logs.add_argument("--limit", type=int, default=20)
    sub.add_parser("clear-logs", help="clear the request log")
    test = sub.add_parser("test", help="run a connectivity preset")
    test.add_argument("preset", nargs="?", default="spot_live", choices=["spot_live", "spot_daily", "tdays"])
    call = sub.add_parser("call", help="run any allowed method, e.g. windctl call wsd 000300.SH close 2024-01-02 2024-01-10")
    call.add_argument("method")
    call.add_argument("args", nargs="*", help="positional args (JSON values allowed)")
    config = sub.add_parser("config", help="show or set config: windctl config [key=value ...]")
    config.add_argument("assignments", nargs="*")
    agent = sub.add_parser("launchagent", help="manage launch-at-login")
    agent.add_argument("action", choices=["install", "uninstall", "status"])
    agent.add_argument("--stop", action="store_true", help="with uninstall: also stop the running daemon")
    router = sub.add_parser("router", help="LAN hub: share this Mac's Wind session with other workstations")
    router_sub = router.add_subparsers(dest="router_command", required=True)
    router_sub.add_parser("status", help="listener state, addresses and workstations")
    enable = router_sub.add_parser("enable", help="start the LAN listener (applies live)")
    enable.add_argument("--host", help="bind address (default 0.0.0.0 = all interfaces)")
    enable.add_argument("--port", type=int, help="TCP port (default 18882)")
    enable.add_argument("--max-inflight", type=int, help="per-workstation concurrent request cap (default 2)")
    router_sub.add_parser("disable", help="stop the LAN listener and hang up open connections")
    add_client = router_sub.add_parser("add-client", help="issue a key for a workstation (shown once)")
    add_client.add_argument("name")
    revoke = router_sub.add_parser("revoke", help="revoke a workstation's key")
    revoke.add_argument("name")
    options = parser.parse_args(argv)

    if options.command == "router":
        # Hub management always targets this Mac's daemon, even in a shell that
        # is set up as a workstation (WIND_BROKER_KEY/URL point at another hub).
        os.environ.pop("WIND_BROKER_KEY", None)
        os.environ.pop("WIND_BROKER_URL", None)
    client = WindClient(client_name="windctl", autospawn=options.command in {"start", "login", "test", "call"})
    try:
        if options.command == "status":
            _print(client.status(), options.json)
        elif options.command == "start":
            ok = spawn_daemon(transport=client.transport)
            _print({"ok": ok, **(client.status() if ok else {})}, options.json)
            return 0 if ok else 1
        elif options.command in {"stop", "restart"}:
            transport: Transport = client.transport
            if transport.healthy():
                client.shutdown_daemon()
                deadline = time.monotonic() + 20
                while transport.healthy(timeout=0.5) and time.monotonic() < deadline:
                    time.sleep(0.2)
            if options.command == "restart":
                if launchagent.is_loaded() and launchagent.kickstart():
                    deadline = time.monotonic() + 20
                    while not transport.healthy(timeout=0.5) and time.monotonic() < deadline:
                        time.sleep(0.2)
                ok = transport.healthy() or spawn_daemon(transport=transport)
                _print({"ok": ok, **(client.status() if ok else {})}, options.json)
                return 0 if ok else 1
            _print({"ok": True, "stopped": not transport.healthy()}, options.json)
        elif options.command == "login":
            _print(client.login(wait=not options.no_wait), options.json)
        elif options.command == "logout":
            _print(client.logout(), options.json)
        elif options.command == "logs":
            entries = client.logs(options.limit)
            if options.json:
                _print(entries, True)
            else:
                for e in entries:
                    print(f"{e['timestamp'][11:23]}  {e['client'][:18]:<18} {e['method']:<7} {e['status']:<12} "
                          f"{e['latency_ms']:>8.0f}ms  {e.get('rows') or '':>5}  {e['args_summary'][:60]}  {e['detail'][:80]}")
        elif options.command == "clear-logs":
            _print(client._request("POST", "/v1/logs/clear"), options.json)
        elif options.command == "test":
            result = client.test(options.preset)
            if options.json:
                _print(result, True)
            else:
                print(f"{result['method']} via {result['source']} in {result['latency_ms']:.0f}ms")
                _table(result["preview"])
        elif options.command == "call":
            frame = client.call(options.method, *[_parse_value(a) for a in options.args])
            if options.json:
                _print(preview(frame, limit=10_000), True)
            else:
                _table(preview(frame))
        elif options.command == "config":
            if options.assignments:
                changes = {}
                for item in options.assignments:
                    key, sep, value = item.partition("=")
                    if not sep:
                        parser.error(f"expected key=value, got {item!r}")
                    changes[key] = _parse_value(value) if value != "" else ""
                _print(client.set_config(**changes), options.json)
            else:
                _print(client.get_config()["config"], options.json)
        elif options.command == "router":
            return _router(client, options)
        elif options.command == "launchagent":
            if options.action == "install":
                _print(launchagent.install(), options.json)
            elif options.action == "uninstall":
                _print(launchagent.uninstall(stop=options.stop), options.json)
            else:
                _print(launchagent.status(), options.json)
    except DaemonUnavailable as exc:
        print(f"windctl: {exc}. Start it with: windctl start", file=sys.stderr)
        return 3
    except ProviderError as exc:
        print(f"windctl: {exc}", file=sys.stderr)
        return 2
    return 0


ENTITLEMENT_WARNING = ("windctl: the LAN router shares this Mac's Wind login with other workstations. "
                       "Make sure your Wind agreement allows it.")


def _router(client: WindClient, options: argparse.Namespace) -> int:
    command = options.router_command
    if command == "status":
        info = client.router_status()
        if options.json:
            _print(info, True)
            return 0
        state = "listening" if info["listening"] else ("enabled, NOT listening" if info["enabled"] else "disabled")
        print(f"router: {state} on {info['host']}:{info['port']}  (cap {info['max_inflight']} per workstation)")
        if info.get("error"):
            print(f" error: {info['error']}")
        if info.get("addresses"):
            print(" reach: " + ", ".join(f"http://{a}:{info['port']}" for a in info["addresses"]))
        for c in info["clients"]:
            print(f"  {c['name']:<20} requests {c['requests']:>6}  inflight {c['inflight']}  "
                  f"last {c['last_seen_at'] or '-'} from {c['last_addr'] or '-'}")
        if not info["clients"]:
            print("  (no workstations; add one with: windctl router add-client NAME)")
    elif command == "enable":
        changes: dict[str, Any] = {"router_enabled": True}
        for option, key in (("host", "router_host"), ("port", "router_port"),
                            ("max_inflight", "router_max_inflight")):
            if getattr(options, option) is not None:
                changes[key] = getattr(options, option)
        print(ENTITLEMENT_WARNING, file=sys.stderr)
        info = client.set_config(**changes)["router"]
        if options.json:
            _print(info, True)
        elif info["listening"]:
            print(f"router listening on {info['host']}:{info['port']}")
        else:
            print(f"router enabled but not listening: {info.get('error')}")
        return 0 if info["listening"] else 1
    elif command == "disable":
        info = client.set_config(router_enabled=False)["router"]
        _print(info, True) if options.json else print("router disabled")
    elif command == "add-client":
        issued = client.router_add_client(options.name)
        info = client.router_status()
        if options.json:
            _print({**issued, "port": info["port"], "addresses": info["addresses"]}, True)
            return 0
        host = (info.get("addresses") or [f"{info.get('hostname', 'this-mac')}.local"])[0]
        print(f"Key for {issued['name']} (shown once, store it now):\n\n  {issued['key']}\n")
        print("On the workstation:")
        print(f"  WIND_BROKER_URL=http://{host}:{info['port']}")
        print(f"  WIND_BROKER_KEY={issued['key']}")
        if not info["listening"]:
            print("\nThe router is not listening yet: windctl router enable")
    elif command == "revoke":
        _print(client.router_revoke(options.name), options.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
