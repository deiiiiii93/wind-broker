# WindBroker for macOS

One persistent WindPy login, shared by every app, service, and notebook on this Mac.

On macOS, WindPy ties the login to the process that called `w.start()`. When that
process exits, the session dies and the next start needs another QR scan.
WindBroker moves the login into a long-lived daemon (`windd`). Everything else
talks to the daemon over loopback HTTP or a Unix socket.

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

The **Control Panel** has four tabs:

1. **Overview & Session** shows the PIDs, state, connected-since time and counters.
   It has Login, Re-authenticate, Disconnect, the daemon controls, and the
   **Launch windd at login** switch.
2. **Live Requests** is an auto-refreshing table of every query from every app.
   It has search, a status filter, refresh, and clear.
3. **Test Console** has the WSQ live quotes, WSD daily close and TDAYS presets.
   It also has a custom runner: pick a method and enter one argument per line.
4. **Configuration** edits `~/.wind-broker/config.json`.

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

## 4. API reference

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
| `busy`, `sdk_missing` | 503 |
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
  by editing `config.json` directly.

Delete the token file and restart windd to rotate it.

## 5. Configuration (`~/.wind-broker/config.json`)

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

`wind_module_path` takes precedence when the file exists. A `WindPy` found on
`sys.path` is the fallback. This matters on this Mac: a copy in
`~/.local/lib/python3.11/site-packages` would otherwise shadow the configured SDK.

Set `WIND_BROKER_HOME` to use another state directory. The tests do this.

## 6. Session model and its trade-offs

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

## 7. CLI

```bash
windctl status | start | stop | restart
windctl login [--no-wait] | logout
windctl logs --limit 20 | clear-logs
windctl test [spot_live|spot_daily|tdays]
windctl call wsd 000300.SH close 2024-01-02 2024-01-10 Fill=Blank
windctl config request_seconds=120 history_cache_dir=~/.wind-broker/history
windctl launchagent install|uninstall|status
```

Without `pip install`, run `PYTHONPATH=src python3 -m wind_broker.cli ...`, or use
the shims in `WindBroker.app/Contents/Resources/bin/`.

## 8. Tests

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
- a detached auto-spawned daemon that outlives its callers.

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
| `~/.wind-broker/pycache` | Python bytecode cache, kept out of the signed app |
| `~/Library/LaunchAgents/com.windbroker.daemon.plist` | Launch-at-login agent |
