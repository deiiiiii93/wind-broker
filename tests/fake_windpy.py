"""Mock of WindPy's ``w`` used by the test suite.

Behaviour is driven by the codes argument so tests can trigger each path:
``ERR``  -> vendor error -40520007     ``HANG`` -> sleeps forever (C++ deadlock)
``CRASH`` -> os._exit (segfault-like)   ``PYERR`` -> raises inside the wrapper
Every ``start``/``stop`` call is appended to $FAKE_WIND_EVENTS so tests can
count logins across processes.
"""

import datetime as dt
import os
import time

import pandas as pd


def _event(name):
    path = os.environ.get("FAKE_WIND_EVENTS")
    if path:
        with open(path, "a") as handle:
            handle.write(f"{name} {os.getpid()}\n")


class WindData:
    def __init__(self, code=0, data=None, codes=(), fields=(), times=()):
        self.ErrorCode, self.Data = code, list(data or [])
        self.Codes, self.Fields, self.Times = list(codes), list(fields), list(times)


class SDK:
    def __init__(self):
        self.connected = False

    def start(self, waitTime=120, **_):
        _event("start")
        delay = float(os.environ.get("FAKE_WIND_LOGIN_DELAY", "0"))
        time.sleep(delay)
        if os.environ.get("FAKE_WIND_LOGIN_FAIL"):
            return WindData(-40520004, ["Login Failed!"])
        self.connected = True
        return WindData(0, ["OK!"])

    def isconnected(self):
        return self.connected

    def stop(self):
        _event("stop")
        self.connected = False

    def _guard(self, codes, usedf=True):
        result = self._guard_df(codes)
        if result is None or usedf:
            return result
        return WindData(result[0], [[str(result[1].iloc[0, 0])]])

    def _guard_df(self, codes):
        if not self.connected:
            return -103, pd.DataFrame({"ERROR": ["not logged in"]})
        if "HANG" in str(codes):
            time.sleep(3600)
        if "CRASH" in str(codes):
            os._exit(11)
        if "PYERR" in str(codes):
            raise TypeError("bad argument type")
        if "ERR" in str(codes):
            return -40520007, pd.DataFrame([["CWSDService: No data."]], columns=[0])
        return None

    def wsd(self, codes, fields, beginTime=None, endTime=None, options=None, usedf=False):
        guarded = self._guard(codes, usedf)
        if guarded:
            return guarded
        days = [dt.date(2024, 1, 2), dt.date(2024, 1, 3)]
        frame = pd.DataFrame({"CLOSE": [3431.1, 3400.5]}, index=days)
        if usedf:
            return 0, frame
        return WindData(0, [[3431.1, 3400.5]], [codes], ["CLOSE"], days)

    def wsq(self, codes, fields, options=None, usedf=False):
        guarded = self._guard(codes, usedf)
        if guarded:
            return guarded
        names = codes.split(",")
        frame = pd.DataFrame({"RT_LAST": [3500.0 + i for i in range(len(names))],
                              "RT_DATE": [dt.datetime(2024, 1, 2)] * len(names),
                              "RT_TIME": [150003.0] * len(names)}, index=names)
        return (0, frame) if usedf else WindData(0, [frame["RT_LAST"].tolist()], names, ["RT_LAST"])

    def wss(self, codes, fields, options=None, usedf=False):
        guarded = self._guard(codes, usedf)
        if guarded:
            return guarded
        return 0, pd.DataFrame({"LASTTRADE_DATE": [dt.datetime(2024, 3, 15)]}, index=[codes])

    def wsi(self, codes, fields, beginTime=None, endTime=None, options=None, usedf=False):
        guarded = self._guard(codes, usedf)
        if guarded:
            return guarded
        index = pd.date_range("2024-01-02 09:45", periods=3, freq="15min")
        return 0, pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=index)

    def wset(self, tablename, options=None, usedf=False):
        guarded = self._guard(tablename, usedf)
        if guarded:
            return guarded
        _event("wset")
        return 0, pd.DataFrame({"wind_code": ["10006000.SH", "10006001.SH"], "strike_price": [3.5, 3.6]})

    def tdays(self, beginTime=None, endTime=None, options=None, usedf=False):
        guarded = self._guard("tdays", usedf)
        if guarded:
            return guarded
        # Real WindPy's tdays ignores usedf for some versions: return WindData.
        times = [dt.datetime(2024, 1, 2), dt.datetime(2024, 1, 3)]
        return WindData(0, [times], ["tdays"], ["tdays"], times)


w = SDK()
