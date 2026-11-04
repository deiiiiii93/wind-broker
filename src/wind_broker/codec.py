"""DataFrame <-> JSON codecs shared by daemon and client.

Two wire formats are produced:

``table``  ``frame.to_json(orient="table", date_format="iso")`` – the documented,
           language-neutral format (falls back to ``split`` when pandas cannot
           express the frame as a table, e.g. duplicate index or a column named
           ``index``).
``exact``  a tagged column-wise encoding that round-trips the index/column
           labels, ``datetime.date`` vs ``datetime`` objects, duplicates and dtypes
           exactly. The Python SDK requests this so a frame from the broker is
           indistinguishable from one returned in-process by WindPy.
"""

from __future__ import annotations

import datetime as dt
import io
import math
from typing import Any

import numpy as np
import pandas as pd

EXACT_VERSION = 1


# --------------------------------------------------------------------- values
def tag(value: Any) -> Any:
    """Convert one Python/numpy value to a JSON-safe tagged form."""

    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        value = float(value)
        if math.isnan(value):
            return None
        if math.isinf(value):
            return {"$f": "inf" if value > 0 else "-inf"}
        return value
    if isinstance(value, str):
        return value
    if value is pd.NaT:
        return {"$nat": True}
    if isinstance(value, pd.Timestamp):
        return {"$ts": value.isoformat()}
    if isinstance(value, np.datetime64):
        return {"$ts": pd.Timestamp(value).isoformat()} if not np.isnat(value) else {"$nat": True}
    if isinstance(value, dt.datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, dt.date):
        return {"$d": value.isoformat()}
    if isinstance(value, dt.time):
        return {"$t": value.isoformat()}
    if isinstance(value, (pd.Timedelta, dt.timedelta)):
        return {"$td": pd.Timedelta(value).isoformat()}
    if isinstance(value, (list, tuple)):
        return {"$l": [tag(item) for item in value]}
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def untag(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "$d" in value:
        return dt.date.fromisoformat(value["$d"])
    if "$dt" in value:
        return dt.datetime.fromisoformat(value["$dt"])
    if "$ts" in value:
        return pd.Timestamp(value["$ts"])
    if "$t" in value:
        return dt.time.fromisoformat(value["$t"])
    if "$td" in value:
        return pd.Timedelta(value["$td"])
    if "$nat" in value:
        return pd.NaT
    if "$f" in value:
        return float(value["$f"])
    if "$l" in value:
        return [untag(item) for item in value["$l"]]
    return value


def tag_tree(value: Any) -> Any:
    """Recursively tag args/kwargs/WindData payloads."""

    if isinstance(value, dict):
        return {str(k): tag_tree(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [tag_tree(v) for v in value]
    return tag(value)


def untag_tree(value: Any) -> Any:
    if isinstance(value, dict):
        if len(value) == 1 and next(iter(value)).startswith("$"):
            return untag(value)
        return {k: untag_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [untag_tree(v) for v in value]
    return value


def plain_args(value: Any) -> Any:
    """Args sent to Wind: dates become ISO strings (WindPy accepts both)."""

    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S") if value.time() != dt.time() else value.strftime("%Y-%m-%d")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [plain_args(v) for v in value]
    if isinstance(value, dict):
        return {k: plain_args(v) for k, v in value.items()}
    if isinstance(value, np.generic):
        return value.item()
    return value


# --------------------------------------------------------------------- frames
def _encode_index(index: pd.Index) -> dict[str, Any]:
    if isinstance(index, pd.MultiIndex):
        return {"levels": [_encode_index(index.get_level_values(i)) for i in range(index.nlevels)],
                "names": [tag(n) for n in index.names]}
    return {"values": [tag(v) for v in index.tolist()], "dtype": str(index.dtype), "name": tag(index.name)}


def _decode_index(payload: dict[str, Any]) -> pd.Index:
    if "levels" in payload:
        arrays = [_decode_index(level) for level in payload["levels"]]
        return pd.MultiIndex.from_arrays(arrays, names=[untag(n) for n in payload["names"]])
    values = [untag(v) for v in payload["values"]]
    return pd.Index(_restore(values, payload["dtype"]), name=untag(payload["name"]), tupleize_cols=False)


def _restore(values: list[Any], dtype: str):
    """Rebuild an array with its original dtype where that is lossless."""

    if dtype.startswith("datetime64"):
        return pd.to_datetime(pd.Series(values, dtype=object)).array
    if dtype in {"object", "str", "string"} or dtype.startswith("string"):
        array = np.empty(len(values), dtype=object)
        array[:] = values
        return array
    if dtype.startswith(("float", "int", "uint", "bool")) and None in values:
        if dtype.startswith("float"):
            return np.array([np.nan if v is None else v for v in values], dtype=dtype)
        return pd.array(values, dtype={"b": "boolean"}.get(dtype[0], "Int64"))
    try:
        return pd.array(values, dtype=dtype)
    except (TypeError, ValueError):
        array = np.empty(len(values), dtype=object)
        array[:] = values
        return array


def encode_exact(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "v": EXACT_VERSION,
        "index": _encode_index(frame.index),
        "columns": _encode_index(frame.columns),
        "dtypes": [str(t) for t in frame.dtypes],
        "data": [[tag(v) for v in frame.iloc[:, i].tolist()] for i in range(frame.shape[1])],
    }


def decode_exact(payload: dict[str, Any]) -> pd.DataFrame:
    if payload.get("v") != EXACT_VERSION:
        raise ValueError(f"unsupported exact frame version {payload.get('v')!r}")
    index = _decode_index(payload["index"])
    columns = _decode_index(payload["columns"])
    series = []
    for values, dtype in zip(payload["data"], payload["dtypes"]):
        series.append(pd.Series(_restore([untag(v) for v in values], dtype), index=index))
    if not series:
        return pd.DataFrame(index=index, columns=columns)
    frame = pd.concat(series, axis=1, ignore_index=True)
    frame.columns = columns
    return frame


def encode_table(frame: pd.DataFrame) -> tuple[str, str]:
    """Return (format, json) using pandas' table orient with a split fallback."""

    try:
        text = frame.to_json(orient="table", date_format="iso")
        # pandas silently turns a duplicate index into a column; detect it.
        if not frame.index.has_duplicates and all(isinstance(c, str) for c in frame.columns):
            return "table", text
    except (ValueError, TypeError):
        pass
    return "split", frame.to_json(orient="split", date_format="iso")


def decode_table(fmt: str, text: str) -> pd.DataFrame:
    return pd.read_json(io.StringIO(text), orient="table" if fmt == "table" else "split")


def encode_frame(frame: pd.DataFrame, formats: str = "table") -> dict[str, Any]:
    """Build the ``frame`` section of a ``/v1/call`` response."""

    body: dict[str, Any] = {"rows": int(frame.shape[0]), "cols": int(frame.shape[1]),
                            "columns": [str(c) for c in frame.columns]}
    if formats in {"table", "both"}:
        body["frame_format"], body["frame"] = encode_table(frame)
    if formats in {"exact", "both"}:
        body["frame_exact"] = encode_exact(frame)
    return body


def decode_frame(body: dict[str, Any]) -> pd.DataFrame:
    if body.get("frame_exact") is not None:
        return decode_exact(body["frame_exact"])
    return decode_table(body.get("frame_format", "table"), body["frame"])


def preview(frame: pd.DataFrame, limit: int = 50) -> dict[str, Any]:
    """Small display-oriented table (all cells stringified) for the app/CLI."""

    head = frame.head(limit)
    index_name = str(frame.index.name) if frame.index.name is not None else ""

    def cell(value: Any) -> str:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return ""
        if isinstance(value, (pd.Timestamp, dt.datetime)) and value == value:
            return value.isoformat(sep=" ") if value.time() != dt.time() else value.date().isoformat()
        return str(value)

    return {
        "columns": [index_name] + [str(c) for c in head.columns],
        "rows": [[cell(idx)] + [cell(v) for v in row] for idx, row in zip(head.index, head.itertuples(index=False))],
        "row_count": int(frame.shape[0]),
        "col_count": int(frame.shape[1]),
        "truncated": bool(frame.shape[0] > limit),
    }
