"""Child process that owns the native WindPy SDK.

The worker is deliberately dumb: it executes one command at a time and reports
success or a structured error. It never decides to exit because a query failed;
only the parent (on a hard timeout) or an explicit ``shutdown`` ends it.

Protocol (multiprocessing.Connection, pickled tuples):
    parent -> worker: (op, payload)
    worker -> parent: (True, result) | (False, {"kind", "code", "message"})
"""

from __future__ import annotations

import importlib.util
import os
import signal
import sys
import traceback
from typing import Any


def load_windpy(module_path: str) -> Any:
    """Import ``w`` from the configured file, else from an installed ``WindPy``.

    The explicit ``wind_module_path`` wins when it exists, so a stray copy in
    site-packages (e.g. ~/.local) can never shadow the configured SDK.
    """

    if not module_path or not os.path.exists(module_path):
        try:
            from WindPy import w  # type: ignore[import-not-found]
            return w
        except ModuleNotFoundError:
            raise FileNotFoundError(f"WindPy not found at {module_path} or in site-packages") from None
    directory = os.path.dirname(module_path)
    if directory not in sys.path:
        # libWind.QuantData.dylib is resolved relative to the module.
        sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location("WindPy", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"WindPy could not be loaded from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["WindPy"] = module
    spec.loader.exec_module(module)
    return module.w


def _error_code(result: Any) -> int:
    code = getattr(result, "ErrorCode", None)
    return int(code) if code is not None else -1


def _message(result: Any) -> str:
    data = getattr(result, "Data", None)
    if isinstance(data, (list, tuple)) and data:
        first = data[0]
        if isinstance(first, (list, tuple)) and first:
            first = first[0]
        return str(first)
    return str(result)


def windata_payload(result: Any) -> dict[str, Any]:
    """Plain-data view of a WindData object (usedf=False calls)."""

    return {
        "ErrorCode": _error_code(result),
        "Codes": list(getattr(result, "Codes", []) or []),
        "Fields": list(getattr(result, "Fields", []) or []),
        "Times": list(getattr(result, "Times", []) or []),
        "Data": [list(row) if isinstance(row, (list, tuple)) else row for row in (getattr(result, "Data", []) or [])],
    }


def windata_to_frame(result: Any):
    """Fallback for SDK functions that ignore ``usedf`` and return WindData."""

    import pandas as pd

    payload = windata_payload(result)
    data, codes, fields, times = payload["Data"], payload["Codes"], payload["Fields"], payload["Times"]
    if not data:
        return pd.DataFrame()
    frame = pd.DataFrame(data).T
    if len(frame.columns) == len(fields):
        frame.columns = fields
    if len(frame.index) == len(times) and len(times) > 1:
        frame.index = times
    elif len(frame.index) == len(codes):
        frame.index = codes
    return frame.infer_objects()


def _frame_message(frame: Any) -> str:
    try:
        values = frame.to_numpy().ravel().tolist()
        return "; ".join(str(v) for v in values if v is not None)[:500] or "no detail"
    except Exception:  # noqa: BLE001 - diagnostics only
        return str(frame)[:500]


def _call(w: Any, method: str, args: list, kwargs: dict, usedf: bool):
    result = getattr(w, method)(*args, **kwargs, usedf=True) if usedf else getattr(w, method)(*args, **kwargs)
    if usedf:
        if isinstance(result, tuple) and len(result) == 2:
            code, frame = result
            if code != 0:
                return False, {"kind": "vendor", "code": int(code),
                               "message": f"Wind {method} error {code}: {_frame_message(frame)}"}
            return True, frame
        if hasattr(result, "ErrorCode"):
            code = _error_code(result)
            if code != 0:
                return False, {"kind": "vendor", "code": code,
                               "message": f"Wind {method} error {code}: {_message(result)}"}
            return True, windata_to_frame(result)
        return False, {"kind": "invalid", "code": None, "message": f"Wind {method} returned an invalid response"}
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
        result = _TupleResult(*result)  # some SDK paths return (code, frame) regardless
    if not hasattr(result, "ErrorCode"):
        return False, {"kind": "invalid", "code": None, "message": f"Wind {method} returned an invalid response"}
    code = _error_code(result)
    if code != 0:
        return False, {"kind": "vendor", "code": code, "message": f"Wind {method} error {code}: {_message(result)}"}
    return True, windata_payload(result)


class _TupleResult:
    def __init__(self, code: int, frame: Any):
        self.ErrorCode = code
        self.Data = [[_frame_message(frame)]] if code else []


def worker_main(connection, module_path: str) -> None:
    # Ctrl-C in a terminal must not kill the SDK out from under the daemon.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    w: Any = None
    try:
        try:
            w = load_windpy(module_path)
            connection.send((True, {"pid": os.getpid()}))
        except Exception as exc:  # noqa: BLE001 - reported to parent
            connection.send((False, {"kind": "sdk_missing", "code": None, "message": f"{type(exc).__name__}: {exc}"}))
            return
        while True:
            op, payload = connection.recv()
            try:
                if op == "login":
                    if w.isconnected():
                        connection.send((True, {"code": 0, "message": "Already connected"}))
                        continue
                    result = w.start(waitTime=int(payload["wait_seconds"]))
                    code = _error_code(result)
                    if code != 0 or not w.isconnected():
                        connection.send((False, {"kind": "login", "code": code,
                                                 "message": f"Wind login failed ({code}): {_message(result)}"}))
                    else:
                        connection.send((True, {"code": 0, "message": _message(result)}))
                elif op == "isconnected":
                    connection.send((True, bool(w.isconnected())))
                elif op == "call":
                    connection.send(_call(w, payload["method"], payload["args"], payload["kwargs"],
                                          payload.get("usedf", True)))
                elif op == "logout":
                    w.stop()
                    connection.send((True, None))
                elif op == "shutdown":
                    try:
                        w.stop()
                    finally:
                        connection.send((True, None))
                    return
                else:
                    connection.send((False, {"kind": "invalid", "code": None, "message": f"unknown op {op!r}"}))
            except Exception as exc:  # noqa: BLE001 - a Python-level failure must not end the session
                connection.send((False, {"kind": "exception", "code": None,
                                         "message": f"{type(exc).__name__}: {exc}",
                                         "trace": traceback.format_exc(limit=5)}))
    except (EOFError, BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        connection.close()
