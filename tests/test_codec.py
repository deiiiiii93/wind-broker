from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from wind_broker.codec import decode_exact, decode_frame, encode_exact, encode_frame, preview


def roundtrip(frame: pd.DataFrame) -> pd.DataFrame:
    return decode_exact(json.loads(json.dumps(encode_exact(frame))))


@pytest.mark.parametrize("frame", [
    pd.DataFrame({"CLOSE": [1.5, np.nan]}, index=[dt.date(2024, 1, 2), dt.date(2024, 1, 3)]),
    pd.DataFrame({"RT_DATE": pd.to_datetime(["2024-01-02"]), "RT_TIME": [150003.0]}, index=["000300.SH"]),
    pd.DataFrame([["CWSDService: No data."]], columns=[0]),
    pd.DataFrame({"A": [1, 2]}, index=["x", "x"]),
    pd.DataFrame({"index": [1]}),
    pd.DataFrame({"D": [dt.date(2024, 1, 2), None], "N": [1, 2], "B": [True, False]}),
    pd.DataFrame({"close": [1.0]}, index=pd.DatetimeIndex(["2024-01-02 09:45"])),
    pd.DataFrame(),
])
def test_exact_roundtrip(frame):
    result = roundtrip(frame)
    pd.testing.assert_frame_equal(result, frame, check_dtype=False, check_index_type=False,
                                  check_column_type=False)
    assert [type(v) for v in result.index] == [type(v) for v in frame.index]


def test_table_fallbacks_to_split_for_unsafe_frames():
    body = encode_frame(pd.DataFrame({"A": [1, 2]}, index=["x", "x"]), "table")
    assert body["frame_format"] == "split"
    assert decode_frame(body).index.tolist() == ["x", "x"]


def test_preview_formats_cells():
    frame = pd.DataFrame({"RT_DATE": pd.to_datetime(["2024-01-02"]), "V": [np.nan]}, index=["000300.SH"])
    table = preview(frame)
    assert table["rows"] == [["000300.SH", "2024-01-02", ""]]
