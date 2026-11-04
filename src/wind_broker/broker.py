"""Session manager: one persistent WindPy worker shared by every client.

Session rules (see README "Session model"):

* ``w.start()`` runs at most once per worker lifetime; later logins are no-ops.
* A query rejected by Wind (``ErrorCode != 0``) or a Python-level exception in
  the SDK wrapper is returned to the caller. The worker and login survive.
* Only a hard deadline breach (C++ deadlock / hang) or a crashed worker kills
  the worker process. A fresh, logged-out worker is spawned immediately and the
  next request logs in again.
* ``w.stop()`` is called only by explicit logout or daemon shutdown.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import itertools
import json
import logging
import multiprocessing
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .codec import decode_exact, encode_exact, plain_args, preview
from .config import DEFAULT_METHODS, LOOPBACK_HOSTS, BrokerConfig, save_config
from .worker import worker_main

log = logging.getLogger("windd")
SHANGHAI = ZoneInfo("Asia/Shanghai")
LOG_CAPACITY = 250
WORKER_BOOT_SECONDS = 60

TEST_CODES = "000300.SH,000016.SH,000905.SH,000852.SH"


class BrokerError(Exception):
    """A failure with an HTTP status and machine-readable kind."""

    STATUS = {"vendor": 422, "exception": 400, "bad_request": 400, "login": 401,
              "timeout": 504, "crashed": 502, "invalid": 502, "busy": 503, "sdk_missing": 503}

    def __init__(self, message: str, *, kind: str, code: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.code = code

    @property
    def http_status(self) -> int:
        return self.STATUS.get(self.kind, 500)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "code": self.code, "message": str(self)}


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def summarize(args: list, kwargs: dict, limit: int = 160) -> str:
    parts = [json.dumps(a, ensure_ascii=False, default=str) if not isinstance(a, str) else a for a in args]
    parts += [f"{k}={v}" for k, v in kwargs.items()]
    text = ", ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Broker:
    def __init__(self, config: BrokerConfig, *, config_file: Path | None = None):
        self.config = config
        self.config_file = config_file
        self.started_at = time.time()
        self._mp = multiprocessing.get_context("spawn")
        # _sdk_lock serializes every interaction with the native SDK.
        # _state_lock guards the small status fields so /v1/status never waits
        # behind a slow Wind query or a pending QR login.
        self._sdk_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._process: Any = None
        self._conn: Any = None
        self.worker_pid: int | None = None
        self.state = "disconnected"
        self.connected_since: float | None = None
        self.last_active_at: float | None = None
        self.last_error: str | None = None
        self.total_requests = 0
        self.failed_requests = 0
        self.login_count = 0
        self.worker_restarts = 0
        self.logs: deque[dict[str, Any]] = deque(maxlen=LOG_CAPACITY)
        self._log_ids = itertools.count(1)
        self._login_thread: threading.Thread | None = None

    # ------------------------------------------------------------ bookkeeping
    def _set(self, **fields: Any) -> None:
        with self._state_lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def record(self, *, client: str, method: str, args_summary: str = "", status: str,
               latency_ms: float = 0.0, rows: int | None = None, cols: int | None = None,
               detail: str = "", count: bool = False) -> dict[str, Any]:
        entry = {
            "id": next(self._log_ids),
            "timestamp": dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "client": client or "unknown",
            "method": method,
            "args_summary": args_summary,
            "rows": rows,
            "cols": cols,
            "latency_ms": round(latency_ms, 1),
            "status": status,
            "detail": detail,
        }
        with self._state_lock:
            self.logs.append(entry)
            if count:
                self.total_requests += 1
                if status not in {"ok", "cache"}:
                    self.failed_requests += 1
        level = logging.INFO if status in {"ok", "cache", "event"} else logging.WARNING
        log.log(level, "%s %s %s %s %.0fms %s", entry["client"], method, status, args_summary, latency_ms, detail)
        return entry

    def recent_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._state_lock:
            items = list(self.logs)
        return items[-max(0, min(limit, LOG_CAPACITY)):][::-1] if limit else []

    def clear_logs(self) -> None:
        with self._state_lock:
            self.logs.clear()

    def sdk_installed(self) -> bool:
        if Path(self.config.wind_module_path).expanduser().exists():
            return True
        try:
            return importlib.util.find_spec("WindPy") is not None
        except (ImportError, ValueError):
            return False

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            alive = self._process is not None and self._process.is_alive()
            return {
                "ok": True,
                "version": __version__,
                "daemon_pid": os.getpid(),
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "started_at": _iso(self.started_at),
                "worker_pid": self.worker_pid if alive else None,
                "worker_alive": alive,
                "worker_restarts": self.worker_restarts,
                "state": self.state,
                "connected": self.state == "connected",
                "connected_since": _iso(self.connected_since),
                "last_active_at": _iso(self.last_active_at),
                "last_error": self.last_error,
                "total_requests": self.total_requests,
                "failed_requests": self.failed_requests,
                "login_count": self.login_count,
                "sdk_installed": self.sdk_installed(),
                "config": self.config.to_dict(),
            }

    # ---------------------------------------------------------------- worker
    def _acquire(self, seconds: float) -> None:
        if not self._sdk_lock.acquire(timeout=seconds):
            raise BrokerError("Wind SDK is busy with another request (login or slow query); retry shortly",
                              kind="busy")

    def _worker_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def _spawn_worker(self) -> None:
        parent, child = self._mp.Pipe()
        process = self._mp.Process(target=worker_main, args=(child, self.config.wind_module_path),
                                   daemon=True, name="windd-worker")
        process.start()
        child.close()
        self._process, self._conn = process, parent
        self._set(worker_pid=process.pid)
        try:
            self._exchange("boot", None, WORKER_BOOT_SECONDS, "Wind worker startup", send=False)
        except BrokerError as exc:
            self._set(state="error", last_error=str(exc))
            self._kill_worker()
            raise
        log.info("worker %s ready (module %s)", process.pid, self.config.wind_module_path)

    def _ensure_worker(self) -> None:
        if not self._worker_alive():
            if self._process is not None:
                self._kill_worker()
                self._set(state="disconnected", connected_since=None)
            self._spawn_worker()

    def _kill_worker(self) -> None:
        process, conn = self._process, self._conn
        self._process = self._conn = None
        if process is not None:
            if process.is_alive():
                process.terminate()
                process.join(2)
                if process.is_alive():
                    process.kill()
            process.join(2)
        if conn is not None:
            conn.close()
        self._set(worker_pid=None)

    def _restart_after(self, reason: str) -> None:
        """Replace a hung/crashed worker with a fresh, logged-out one."""

        self._kill_worker()
        self._set(state="error", connected_since=None, last_error=reason,
                  worker_restarts=self.worker_restarts + 1)
        self.record(client="windd", method="worker", status="event", detail=f"worker restarted: {reason}")
        try:
            self._spawn_worker()
        except BrokerError:
            pass  # state/last_error already describe the failure

    def _exchange(self, op: str, payload: Any, seconds: float, what: str, *, send: bool = True) -> Any:
        conn = self._conn
        if conn is None:
            raise BrokerError(f"{what}: worker is not running", kind="crashed")
        try:
            if send:
                conn.send((op, payload))
            ready = conn.poll(seconds)
        except (OSError, EOFError, BrokenPipeError) as exc:
            self._restart_after(f"{what}: worker pipe failed ({exc})")
            raise BrokerError(f"{what}: Wind worker exited unexpectedly", kind="crashed") from exc
        if not ready:
            message = f"{what} timed out after {seconds:g}s; worker restarted"
            self._restart_after(message)
            raise BrokerError(message, kind="timeout")
        try:
            ok, result = conn.recv()
        except (EOFError, OSError) as exc:
            self._restart_after(f"{what}: worker crashed")
            raise BrokerError(f"{what}: Wind worker exited unexpectedly", kind="crashed") from exc
        if not ok:
            raise BrokerError(result.get("message", "Wind error"), kind=result.get("kind", "exception"),
                              code=result.get("code"))
        return result

    # ----------------------------------------------------------------- login
    def _login_locked(self, wait_seconds: float | None = None) -> None:
        if self.state == "connected" and self._worker_alive():
            return
        wait = int(wait_seconds or self.config.startup_seconds)
        self._set(state="connecting", last_error=None)
        started = time.monotonic()
        try:
            self._ensure_worker()
            self._set(login_count=self.login_count + 1)
            result = self._exchange("login", {"wait_seconds": wait}, wait + 15, "Wind login")
        except BrokerError as exc:
            if exc.kind != "timeout":  # timeout already set state via _restart_after
                self._set(state="error", connected_since=None, last_error=str(exc))
            self.record(client="windd", method="login", status="error", detail=str(exc),
                        latency_ms=(time.monotonic() - started) * 1000)
            raise
        now = time.time()
        self._set(state="connected", connected_since=now, last_active_at=now, last_error=None)
        self.record(client="windd", method="login", status="event",
                    detail=str(result.get("message", "connected")), latency_ms=(time.monotonic() - started) * 1000)

    def login(self, *, wait: bool = True, wait_seconds: float | None = None) -> dict[str, Any]:
        if self.state == "connected" and self._worker_alive():
            return self.status()
        if not wait:
            with self._state_lock:
                busy = self._login_thread is not None and self._login_thread.is_alive()
                if not busy:
                    self.state = "connecting"
                    self._login_thread = threading.Thread(target=self._background_login, args=(wait_seconds,),
                                                          name="windd-login", daemon=True)
                    self._login_thread.start()
            return self.status()
        self._acquire(self.config.startup_seconds + self.config.request_seconds + 30)
        try:
            self._login_locked(wait_seconds)
        finally:
            self._sdk_lock.release()
        return self.status()

    def _background_login(self, wait_seconds: float | None) -> None:
        try:
            self.login(wait=True, wait_seconds=wait_seconds)
        except BrokerError:
            pass  # recorded in state/logs

    def logout(self) -> dict[str, Any]:
        self._acquire(self.config.request_seconds + 30)
        try:
            if self._worker_alive():
                try:
                    # shutdown == w.stop() + exit; a fresh worker is a clean reset.
                    self._exchange("shutdown", None, 15, "Wind logout")
                except BrokerError:
                    pass
            self._kill_worker()
            self._set(state="disconnected", connected_since=None, last_error=None)
            self.record(client="windd", method="logout", status="event", detail="w.stop() called")
            try:
                self._spawn_worker()
            except BrokerError:
                pass
        finally:
            self._sdk_lock.release()
        return self.status()

    def start_background(self) -> None:
        """Daemon boot: spawn the (logged-out) worker, optionally log in."""

        def boot() -> None:
            if self.config.auto_connect_on_start:
                self.login(wait=False)
                return
            if not self._sdk_lock.acquire(timeout=5):
                return
            try:
                self._ensure_worker()
            except BrokerError:
                pass
            finally:
                self._sdk_lock.release()

        threading.Thread(target=boot, name="windd-boot", daemon=True).start()

    def shutdown(self) -> None:
        acquired = self._sdk_lock.acquire(timeout=5)
        try:
            if self._worker_alive() and acquired:
                try:
                    self._exchange("shutdown", None, 10, "Wind shutdown")
                except BrokerError:
                    pass
            self._kill_worker()
            self._set(state="disconnected", connected_since=None)
        finally:
            if acquired:
                self._sdk_lock.release()

    # ----------------------------------------------------------------- calls
    def _validate(self, method: str, args: Any, kwargs: Any) -> None:
        if method not in DEFAULT_METHODS:
            raise BrokerError(f"Unsupported Wind method {method!r}; allowed: {', '.join(DEFAULT_METHODS)}",
                              kind="bad_request")
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise BrokerError("args must be a list and kwargs an object", kind="bad_request")
        if "func" in kwargs:
            raise BrokerError("Callback subscriptions (func=...) cannot cross the broker; poll instead",
                              kind="bad_request")
        if "usedf" in kwargs:
            raise BrokerError("usedf is controlled by the broker; pass it at the top level", kind="bad_request")

    def _cache_path(self, method: str, args: list, kwargs: dict) -> Path | None:
        directory = self.config.cache_dir
        if directory is None or method not in {"wsd", "wset"}:
            return None
        today = dt.datetime.now(SHANGHAI).date().isoformat()
        historical = (
            (method == "wsd" and len(args) >= 4 and str(args[3])[:10] < today)
            or (method == "wset" and len(args) >= 2 and any(
                part.strip().startswith("date=") and part.strip()[5:15] < today for part in str(args[1]).split(";")))
        )
        if not historical:
            return None
        key = hashlib.sha256(json.dumps([method, args, kwargs], sort_keys=True, default=str).encode()).hexdigest()
        return directory / f"{key}.json"

    def call(self, method: str, args: list | None = None, kwargs: dict | None = None, *, client: str = "unknown",
             request_seconds: float | None = None, usedf: bool = True) -> tuple[Any, str]:
        """Run one SDK call. Returns (DataFrame | WindData payload, source)."""

        call_args: list = plain_args(list(args or []))
        call_kwargs: dict = plain_args(dict(kwargs or {}))
        summary = summarize(call_args, call_kwargs)
        started = time.monotonic()
        try:
            self._validate(method, call_args, call_kwargs)
            seconds = float(request_seconds or self.config.request_seconds)
            if not 0 < seconds <= 3600:
                raise BrokerError("request_seconds must be in (0, 3600]", kind="bad_request")
        except BrokerError as exc:
            self.record(client=client, method=method, args_summary=summary, status="error",
                        detail=str(exc), count=True)
            raise

        cache_path = self._cache_path(method, call_args, call_kwargs) if usedf else None
        if cache_path is not None and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
                if cached.get("request") == [method, call_args, call_kwargs]:
                    frame = decode_exact(cached["frame"])
                    self.record(client=client, method=method, args_summary=summary, status="cache",
                                rows=frame.shape[0], cols=frame.shape[1], count=True,
                                latency_ms=(time.monotonic() - started) * 1000,
                                detail=f"history cache {cached.get('captured_at', '')}")
                    return frame, "cache"
            except (ValueError, KeyError, OSError) as exc:
                log.warning("ignoring unreadable cache entry %s: %s", cache_path, exc)

        try:
            self._acquire(self.config.startup_seconds + seconds + 30)
            try:
                if self.state != "connected" or not self._worker_alive():
                    self._login_locked()  # auto-connect on first use
                result = self._exchange("call", {"method": method, "args": call_args, "kwargs": call_kwargs, "usedf": usedf},
                                        seconds, f"Wind {method}")
            except BrokerError as exc:
                if exc.kind == "vendor":
                    self._probe_connection()
                raise
            finally:
                self._sdk_lock.release()
        except BrokerError as exc:
            status = {"vendor": "vendor_error", "timeout": "timeout"}.get(exc.kind, "error")
            self.record(client=client, method=method, args_summary=summary, status=status, detail=str(exc),
                        latency_ms=(time.monotonic() - started) * 1000, count=True)
            raise

        self._set(last_active_at=time.time())
        latency = (time.monotonic() - started) * 1000
        if usedf:
            rows, cols = int(result.shape[0]), int(result.shape[1])
        else:
            data = result.get("Data") or []
            rows, cols = (len(data[0]) if data and isinstance(data[0], list) else 0), len(data)
        self.record(client=client, method=method, args_summary=summary, status="ok", rows=rows, cols=cols,
                    latency_ms=latency, count=True)
        if cache_path is not None:
            self._write_cache(cache_path, method, call_args, call_kwargs, result)
        return result, "wind"

    def _probe_connection(self) -> None:
        """After a vendor error, check (never reset) the login. Lock is held."""

        try:
            connected = self._exchange("isconnected", None, 10, "Wind isconnected")
        except BrokerError:
            return
        if not connected:
            self._set(state="disconnected", connected_since=None,
                      last_error="Wind reported the session as disconnected; next request logs in again")

    def _write_cache(self, path: Path, method: str, args: list, kwargs: dict, frame: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({
                "request": [method, args, kwargs],
                "captured_at": dt.datetime.now(SHANGHAI).isoformat(),
                "frame": encode_exact(frame),
            }))
            temporary.replace(path)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("history cache write failed: %s", exc)

    # ----------------------------------------------------------------- tests
    def run_test(self, preset: str | None, method: str | None = None, args: list | None = None,
                 kwargs: dict | None = None, client: str = "windbroker-test") -> dict[str, Any]:
        today = dt.datetime.now(SHANGHAI).date()
        presets = {
            "spot_live": ("wsq", [TEST_CODES, "rt_last,rt_date,rt_time"], {}),
            "spot_daily": ("wsd", [TEST_CODES, "close", (today - dt.timedelta(days=10)).isoformat(),
                                   today.isoformat(), "Fill=Blank"], {}),
            "tdays": ("tdays", [(today - dt.timedelta(days=30)).isoformat(), today.isoformat(), ""], {}),
        }
        if preset and preset != "custom":
            if preset not in presets:
                raise BrokerError(f"unknown preset {preset!r}; choose {', '.join(presets)} or custom",
                                  kind="bad_request")
            method, args, kwargs = presets[preset]
        if not method:
            raise BrokerError("custom test requires a method", kind="bad_request")
        started = time.monotonic()
        frame, source = self.call(method, list(args or []), dict(kwargs or {}), client=client)
        return {"ok": True, "preset": preset or "custom", "method": method, "args": args, "source": source,
                "latency_ms": round((time.monotonic() - started) * 1000, 1), "preview": preview(frame)}

    # ---------------------------------------------------------------- config
    def update_config(self, changes: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(changes, dict):
            raise BrokerError("config body must be an object", kind="bad_request")
        if "host" in changes and changes["host"] not in LOOPBACK_HOSTS:
            raise BrokerError("a non-loopback host can only be set by editing config.json directly",
                              kind="bad_request")
        try:
            candidate = self.config.updated(changes)
        except (TypeError, ValueError) as exc:
            raise BrokerError(str(exc), kind="bad_request") from exc
        restart = sorted(k for k in BrokerConfig.RESTART_KEYS
                         if getattr(candidate, k) != getattr(self.config, k))
        save_config(candidate, self.config_file)
        # Timeouts, cache dir and auto-connect apply live; listener and SDK
        # paths are kept until restart so the running session is untouched.
        live = {k: getattr(candidate, k) for k in ("startup_seconds", "request_seconds",
                                                    "auto_connect_on_start", "history_cache_dir")}
        for key, value in live.items():
            setattr(self.config, key, value)
        return {"ok": True, "config": candidate.to_dict(), "restart_required": restart}
