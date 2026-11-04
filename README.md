# WindBroker for macOS

One persistent WindPy login, shared by every app, service, and notebook on this Mac.

On macOS, WindPy ties the login to the process that called `w.start()`. When that
process exits, the session dies and the next start needs another QR scan.
WindBroker moves the login into a long-lived daemon (`windd`). Everything else
talks to the daemon over loopback HTTP or a Unix socket. With the opt-in
[LAN router](#4-share-with-other-workstations-lan-router), other workstations on
your network can use the same session too.

```
 index-dashboard-codex ─┐
 notebooks (w proxy)  ──┼──► windd (HTTP 127.0.0.1:18881 + ~/.wind-broker/wind.sock)
 scripts / windctl    ──┤        │  one lock, one queue
 WindBroker.app       ──┘        ▼
                          worker process ── WindPy ── libWind.QuantData.dylib
                          (w.start() once, kept alive)
```

| Component | Location |
|---|---|
| Daemon `windd` | `src/wind_broker/daemon.py`, `broker.py`, `worker.py` |
| Client SDK | `src/wind_broker/client.py` (`WindClient`), `proxy.py` (`w`) |
| CLI `windctl` | `src/wind_broker/cli.py` |
| macOS app | `macos/Sources/*.swift`, built by `scripts/build_app.sh` |
| LAN router (hub mode) | `src/wind_broker/router.py` |
| Tests (mock WindPy) | `tests/` |

## 1. Install

The only runtime dependency is pandas. The daemon needs a Python that has pandas.
Your anaconda Python, `/opt/anaconda3/bin/python3`, works.

```bash
cd ~/wind-broker
./scripts/build_app.sh            # -> dist/WindBroker.app (swiftc, arm64, macOS 14+)
./scripts/install_app.sh          # copies to /Applications, links windctl/windd into ~/.local/bin
# or: ./scripts/install_app.sh --symlink   (dev: /Applications/WindBroker.app -> dist/)
open -a WindBroker
```

The app bundles its own copy of the Python package in
`WindBroker.app/Contents/Resources/python`, so it does not need `pip install`.

For Python consumers, install the SDK into each environment that imports it:

```bash
pip install -e ~/wind-broker                       # anaconda / any venv
cd ~/index-dashboard-codex && uv pip install -e ~/wind-broker
```

## 2. Use the app

The **menu bar icon** shows the session state.

| Color | Meaning |
|---|---|
| Green | Connected |
| Amber | Connecting, waiting for the QR scan |
| Gray | Disconnected, or the daemon is stopped (outline icon) |
| Red | Error |

The popover shows uptime and request counters. It has Connect and Disconnect
buttons, Start, Stop and Restart for the daemon, and the last five requests.

The **Control Panel** has five tabs:

1. **Overview & Session** shows the PIDs, state, connected-since time and counters.
   It has Login, Re-authenticate, Disconnect, the daemon controls, and the
   **Launch windd at login** switch.
2. **Live Requests** is an auto-refreshing table of every query from every app.
   It has search, a status filter, refresh, and clear.
3. **Test Console** has the WSQ live quotes, WSD daily close and TDAYS presets.
   It also has a custom runner: pick a method and enter one argument per line.
4. **Configuration** edits `~/.wind-broker/config.json`.
5. **Router** turns the [LAN router](#4-share-with-other-workstations-lan-router)
   on and off and sets its port. It also adds and revokes workstation keys, and shows
   each key once.

Quitting the app leaves `windd` running, so other apps stay connected.

### Launch at login

Turn on **Launch windd at login** in the Overview tab, or run:

```bash
windctl launchagent install      # writes ~/Library/LaunchAgents/com.windbroker.daemon.plist
windctl launchagent status
windctl launchagent uninstall    # stops future auto-start; the running daemon keeps its session
```

The LaunchAgent restarts `windd` if it crashes. It does not restart it after a
clean stop or `/v1/shutdown`. It runs in your GUI session so the Wind QR window
can appear. Set `auto_connect_on_start: true` to log in as soon as the daemon starts.

## 3. Connect your code

### index-dashboard-codex

The dashboard's `WindProvider` only calls `client.call(method, *args)` and
`client.close()`. Swap the transport in
`app/providers/wind_client.py`, keeping the old file as a fallback if you like:

```python
"""WindPy transport backed by the machine-wide WindBroker daemon."""

from wind_broker import WindClient as _BrokerClient

from app.providers.base import ProviderError


class WindClient(_BrokerClient):
    # The dashboard catches its own ProviderError type.
    error_class = ProviderError
```

The constructor accepts the same `module_path`, `startup_seconds`,
`request_seconds` and `history_cache_dir` keywords, so `app/main.py` needs no change.

- **Vendor errors** such as `-40520007` raise the dashboard's `ProviderError`, with `.code` set.
- **Closing** with `client.close()` never logs Wind out.
- **Auto-start:** if `windd` is down, the first call starts it detached in its own session, so it outlives the dashboard.
- **History cache:** set `history_cache_dir` in `~/.wind-broker/config.json` to cache completed historical `wsd`/`wset` results in the daemon, for all apps.

This was verified against the dashboard's real `WindProvider`, running from its
`.venv` on Python 3.12 with pandas 3, talking to a daemon on anaconda Python 3.11 with pandas 2.2.

### Scripts and notebooks

Replace `from WindPy import w` with `from wind_broker import w`. Nothing else changes:

```python
from wind_broker import w

w.start()                                   # instant if windd is already logged in
data = w.wsd("000300.SH", "close", "2024-01-02", "2024-01-31")   # WindData-like (.ErrorCode, .Data, .Times...)
err, df = w.wsq("000300.SH,000905.SH", "rt_last,rt_time", usedf=True)
w.stop()                                    # no-op: never ends the shared session
w.logout()                                  # explicit, for every app on this Mac
```

Like WindPy, the `w` proxy returns vendor errors as `ErrorCode != 0` instead of raising.
`WindClient` raises `ProviderError`. Real-time callbacks such as `wsq(..., func=cb)`
cannot cross processes, so poll `w.wsq()` instead.

The proxy supports `wsd`, `wss`, `wsq`, `wsi`, `wset`, `tdays`, `tdaysoffset`,
`tdayscount`, `edb`, `wst`, `wses` and `wsee`.

### Any language: HTTP

Over the Unix socket, no token is needed:

```bash
curl -s --unix-socket ~/.wind-broker/wind.sock http://localhost/v1/status
```

Over TCP, send the per-user token from `~/.wind-broker/token`:

```bash
TOKEN=$(cat ~/.wind-broker/token)
curl -s --noproxy '*' -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18881/v1/status
curl -s --noproxy '*' -X POST http://127.0.0.1:18881/v1/call \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -H 'X-Wind-Client: my-app' \
  -d '{"method":"wsd","args":["000300.SH","close","2024-01-02","2024-01-10","Fill=Blank"]}'
```

Use `--noproxy '*'` if `http_proxy` is set in your shell. The response's `frame`
field is `DataFrame.to_json(orient="table", date_format="iso")`.

## 4. Share with other workstations (LAN router)

The Mac that runs Wind can act as a **hub**. An opt-in second listener inside
`windd` lets other machines on your local network send Wind data requests. Those
machines can be Macs, Windows PCs or Linux boxes, and none of them needs Wind
installed. They all share the hub's one logged-in session.

```
 workstation A (Windows, Python)  ─┐  http://hub-mac.local:18882
 workstation B (curl / R / Excel) ─┼─────────────────────────────►  windd router listener ─┐
 workstation C (Mac notebook)     ─┘   Authorization: Bearer wbk_…                         │
                                                                     local apps ──► windd ─┴─► one WindPy session
```

> **Wind entitlement.** The router shares one Wind login with other people and
> machines. Make sure your Wind agreement allows that before you turn it on.
> WindBroker does not check this for you.

### On the hub (the Mac with Wind)

```bash
windctl router enable                 # listens on 0.0.0.0:18882, applies live, keeps the session
windctl router add-client alice-pc    # prints the key ONCE, plus the two lines for the workstation
windctl router status                 # addresses to use, workstations, requests, in-flight counts
windctl router revoke alice-pc        # the key stops working on the next request
windctl router disable                # closes the listener and hangs up open connections
```

The **Router** tab in the Control Panel does the same things.

- **Off by default.** Nothing listens on the LAN until you enable the router.
  Turning it on or off, or changing its port, never restarts `windd` or logs Wind out.
- **One key per workstation.** Keys look like `wbk_…`. They are shown once and
  stored only as SHA-256 digests in `~/.wind-broker/router_clients.json` (mode 0600).
  If a key is lost, revoke it and add the workstation again.
- **Data only.** Workstations can call `POST /v1/call` and read a trimmed
  `GET /v1/status` (state, connected, timestamps). Everything else is local-only
  and returns 403 over the router: login, logout, config, logs, test, shutdown,
  and key management.
- **The hub owner logs in.** A workstation can never start a Wind login, so no QR
  window ever pops up because of a colleague. While the hub is logged out, remote
  calls return 503 `not_connected`, except history-cache hits.
- **Fair sharing.** Each workstation may have `router_max_inflight` requests
  (default 2) in flight or queued. More get HTTP 429 `busy`. Local apps are never
  capped. A workstation may shorten the hub's `request_seconds`, but not extend it.
- **Live Requests** shows remote queries as `<workstation>/<app>`, for example `alice-pc/notebook`.

### On a workstation

Python, on any OS:

```bash
pip install "git+https://github.com/deiiiiii93/wind-broker"   # or: pip install -e <path to a copy of this repo>
```

```python
import os
os.environ["WIND_BROKER_URL"] = "http://192.168.1.20:18882"   # or set these in the shell
os.environ["WIND_BROKER_KEY"] = "wbk_…"

from wind_broker import w, WindClient
w.start()                                    # reports the hub's session; never logs in
data = w.wsd("000300.SH", "close", "2024-01-02", "2024-01-31")
frame = WindClient().call("wss", "IF2403.CFE", "lasttrade_date")
# or explicitly: WindClient(base_url="http://192.168.1.20:18882", api_key="wbk_…")
```

With a key set, the SDK is in **remote mode**. It talks only to the hub URL and
never starts a local `windd`, and it never sends this machine's local token
anywhere. Remote mode has no macOS-only dependencies. A key without a URL is a
configuration error.

Any other language uses the same JSON API as the local one, with the key as a
bearer token:

```bash
curl -s -X POST http://192.168.1.20:18882/v1/call \
  -H "Authorization: Bearer wbk_…" -H 'Content-Type: application/json' \
  -d '{"method":"wsd","args":["000300.SH","close","2024-01-02","2024-01-10"],"client":"excel"}'
```

### Network notes

- **Plain HTTP, trusted LAN only.** Keys and data travel unencrypted. Use the router
  on an office or home network you trust, not on public Wi-Fi or the internet.
  Don't forward its port on your router.
- **Firewall.** If the macOS application firewall is on, allow incoming connections
  for the Python that runs `windd`. Otherwise workstations time out.
  To restrict the listener, set `router_host` to a single interface address.
- **Errors** use the same envelope as the local API, plus `unauthorized` (401),
  `forbidden` (403), `busy` (429 over the cap) and `not_connected` (503).
- **Limits.** Idle connections are closed after 30 s, and at most 64 router
  connections are open at once. Problems with the router never stop `windd`: a
  busy port or an unreadable key file shows up as `error` in `windctl router status`.

## 5. API reference

| Endpoint | Purpose |
|---|---|
| `GET /v1/status`, `/v1/health` | PIDs, uptime, `state`, `connected`, `connected_since`, `last_active_at`, `last_error`, counters, `sdk_installed`, `config` |
| `POST /v1/login` `{"wait": true, "wait_seconds": 300}` | Runs `w.start()` once. `wait:false` returns immediately with `state: connecting`. |
| `POST /v1/logout` | Calls `w.stop()` and resets to a fresh, logged-out worker |
| `POST /v1/call` | `{"method","args","kwargs","client","request_seconds","usedf","frame_format"}`. Auto-connects on first use. |
| `GET /v1/logs?limit=100` | Newest-first ring buffer of 250 entries |
| `POST /v1/logs/clear` | Empties the log |
| `POST /v1/test` | `{"preset":"spot_live"\|"spot_daily"\|"tdays"}` or `{"preset":"custom","method","args"}`. Returns a preview table. |
| `GET /v1/config`, `POST /v1/config` | Reads or persists config. The response lists `restart_required` keys. |
| `POST /v1/shutdown` | Stops the worker with `w.stop()`, then the daemon |
| `GET /v1/router` | LAN router state: `enabled`, `host`, `port`, `listening`, `error`, `max_inflight`, `addresses`, `clients` |
| `POST /v1/router/clients` `{"name"}` | Issues a workstation key: `{"name","key"}`. The key is returned only this once. |
| `POST /v1/router/clients/revoke` `{"name"}` | Revokes a workstation key (404 if unknown) |

`/v1/call` options:

- **`frame_format`** is `table` (default), `exact`, or `both`. `exact` is a typed
  encoding the Python SDK uses. It round-trips `datetime.date` indexes, duplicate
  labels, and integer column names, which pandas' table orient loses or rejects.
- **`usedf: false`** returns the raw WindData fields (`ErrorCode`, `Codes`, `Fields`, `Times`, `Data`).

Errors return `{"ok": false, "error": {"kind", "code", "message"}}` with these statuses:

| Kind | HTTP status |
|---|---|
| `vendor` | 422 |
| `bad_request` | 400 |
| `login` | 401 |
| `crashed` | 502 |
| `busy`, `sdk_missing`, `not_connected` | 503 |
| `timeout` | 504 |

Access control:

- **TCP needs the token.** Every account on a Mac can reach `127.0.0.1`. So each
  TCP request must send `Authorization: Bearer <token>`. The daemon creates the
  token on first start in `~/.wind-broker/token`, readable only by you. Without
  it, other local users could read your Wind data or change the SDK path to run
  their code as you. The Python SDK and the app send the token automatically.
- **The Unix socket needs no token.** It is mode 0600 inside the 0700
  `~/.wind-broker` directory, so only you can connect.
- **Browser requests are blocked.** POSTs must send `Content-Type: application/json`
  and a loopback Host header, which defeats cross-site and DNS-rebinding attacks.
- **No remote exposure through the API.** A non-loopback `host` can only be set
  by editing `config.json` directly. The only supported way to serve other
  machines is the separate, key-protected [LAN router](#4-share-with-other-workstations-lan-router).

Delete the token file and restart windd to rotate it.

## 6. Configuration (`~/.wind-broker/config.json`)

| Key | Default | Applies |
|---|---|---|
| `host` | `127.0.0.1` | restart |
| `port` | `18881` | restart |
| `unix_socket_path` | `~/.wind-broker/wind.sock` | restart |
| `wind_module_path` | `/Applications/Wind API.app/Contents/python/WindPy.py` | restart |
| `python_executable` | interpreter that first wrote the config | restart |
| `startup_seconds` | `300` (QR scan window) | live |
| `request_seconds` | `90.0` | live |
| `auto_connect_on_start` | `false` | next start |
| `history_cache_dir` | `null` | live |
| `router_enabled` | `false` | live |
| `router_host` | `0.0.0.0` | live |
| `router_port` | `18882` (must differ from `port`) | live |
| `router_max_inflight` | `2` | live |

`wind_module_path` takes precedence when the file exists. A `WindPy` found on
`sys.path` is the fallback. This keeps a stray copy of `WindPy.py` in a
site-packages directory, such as `~/.local/lib/python3.x/site-packages`, from
shadowing the configured SDK.

Set `WIND_BROKER_HOME` to use another state directory. The tests do this.

## 7. Session model and its trade-offs

- **One login:** `w.start()` runs once per worker. Later logins are no-ops, from any number of clients.
- **Rejected queries are harmless.** A vendor error such as `-40522012` or
  `-40520007`, or a Python exception inside WindPy, goes back to the caller.
  The worker and the login survive. After a vendor error the broker asks
  `w.isconnected()` to keep the status honest, and never resets the session.
- **Only a hang kills the worker.** If a call runs past `request_seconds`, the
  caller gets a `timeout`, the worker is killed, and a fresh logged-out worker
  starts at once. The next request logs in again. With Wind's remembered
  credentials that may be silent; otherwise it needs a QR scan. The HTTP daemon
  keeps answering throughout.
- **Calls are serialized.** One lock guards the SDK, so a slow query delays the
  others. WindPy's wrapper only locks its callback table, not the data calls.
  More parallelism would need more logged-in workers, which means more logins.
  Size `request_seconds` above your longest legitimate history pull, or a slow
  pull will be treated as a hang.
- **`w.stop()` is called only** by explicit logout, Re-authenticate, or daemon shutdown.

## 8. CLI

```bash
windctl status | start | stop | restart
windctl login [--no-wait] | logout
windctl logs --limit 20 | clear-logs
windctl test [spot_live|spot_daily|tdays]
windctl call wsd 000300.SH close 2024-01-02 2024-01-10 Fill=Blank
windctl config request_seconds=120 history_cache_dir=~/.wind-broker/history
windctl launchagent install|uninstall|status
windctl router status | enable [--host H] [--port N] [--max-inflight N] | disable
windctl router add-client NAME | revoke NAME
```

`windctl router …` always manages this Mac's daemon, even in a shell where
`WIND_BROKER_URL`/`WIND_BROKER_KEY` point at another hub. Other commands such as
`windctl call` honor those variables, so they also work from a workstation.

Without `pip install`, run `PYTHONPATH=src python3 -m wind_broker.cli ...`, or use
the shims in `WindBroker.app/Contents/Resources/bin/`.

## 9. Tests

```bash
python3 -m pytest -q
```

The tests use a mock `WindPy` in `tests/fake_windpy.py`. They never load the real
SDK: `PYTHONNOUSERSITE=1` is set and the module path is explicit. They run a real
HTTP daemon and a real worker process, and cover:

- one `w.start()` across many clients, threads, `close()` and `w.stop()` calls;
- frames from all six core methods, plus the table wire format;
- vendor errors and Python exceptions leaving the session alive;
- hung and crashed workers being replaced while the daemon keeps serving;
- a status call that does not block behind a slow query;
- async login, login failure, auto-connect and the history cache;
- test presets, config persistence, the log ring buffer and the Unix socket;
- browser-request guards, the per-user TCP token, and the `w` proxy semantics;
- a detached auto-spawned daemon that outlives its callers;
- the LAN router: key auth and revocation, local-only admin routes, the per-workstation
  cap, no remote logins, live enable/disable (open connections included), non-fatal bind
  and registry errors, SDK remote mode, and a Windows-safe import (no `fcntl`).

Build check:

```bash
./scripts/build_app.sh && file dist/WindBroker.app/Contents/MacOS/WindBroker
# -> Mach-O 64-bit executable arm64
```

## Files and logs

| Path | Contents |
|---|---|
| `~/.wind-broker/config.json` | Configuration |
| `~/.wind-broker/windd.log` | Daemon output, plus WindPy's banner |
| `~/.wind-broker/windd.pid` | Daemon PID |
| `~/.wind-broker/wind.sock` | Unix socket |
| `~/.wind-broker/token` | Per-user API token for TCP clients (mode 0600) |
| `~/.wind-broker/router_clients.json` | LAN router workstation names and key digests (mode 0600) |
| `~/.wind-broker/pycache` | Python bytecode cache, kept out of the signed app |
| `~/Library/LaunchAgents/com.windbroker.daemon.plist` | Launch-at-login agent |

## License

WindBroker is released under the [MIT License](LICENSE).

WindPy, `libWind.QuantData.dylib`, and Wind data are proprietary to Wind
Information Co., Ltd. and are not included or covered by this license. Using
WindBroker requires your own Wind terminal installation and API entitlement,
and your use of Wind data remains subject to your Wind agreement.
