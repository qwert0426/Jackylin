"""臺灣集中保管結算所（TDCC）集保戶股權分散表。

- 開放資料 getOD.ashx?id=1-5：最新一週、全市場，一次下載。
- 個股查詢 qryStock：可查最近約一年（約 51 週）每週資料，一次一檔一週，用來回補。

持股分級 level：1=1-999 股 … 12=400,001-600,000 股（400 張）… 15=1,000,001 股以上，
16=差異數調整，17=合計。400 張以上大戶 = level 12~15。
"""
from __future__ import annotations

import io
import re
import time

import pandas as pd
import requests

from ..util import classify_id, log, to_num

OPEN_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
QRY_URL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
COLS = ["date", "stock_id", "level", "people", "shares", "percent"]
_LEVEL_LOW = [1, 1000, 5001, 10001, 15001, 20001, 30001, 40001, 50001, 100001, 200001, 400001,
              600001, 800001, 1000001]


def range_to_level(text) -> float:
    """'400,001-600,000' → 12；'1,000,001以上' → 15；差異數調整 → 16；合計 → 17。"""
    s = str(text)
    if "合" in s or "total" in s.lower():
        return 17.0
    if "差異" in s or "adjust" in s.lower():
        return 16.0
    m = re.search(r"[\d,]+", s)
    if not m:
        return float("nan")
    low = int(m.group(0).replace(",", ""))
    return float(_LEVEL_LOW.index(low) + 1) if low in _LEVEL_LOW else float("nan")


def latest_week() -> pd.DataFrame:
    r = requests.get(OPEN_URL, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.content.decode("utf-8-sig")), dtype=str)
    df.columns = ["date", "stock_id", "level", "people", "shares", "percent"][: len(df.columns)]
    df["stock_id"] = df["stock_id"].str.strip()
    df = df[df["stock_id"].map(lambda s: classify_id(s) is not None)]
    df["date"] = pd.to_datetime(df["date"].str.strip(), format="%Y%m%d").dt.strftime("%Y-%m-%d")
    for c in ["level", "people", "shares", "percent"]:
        df[c] = df[c].map(to_num).astype("float64")
    return df[COLS].reset_index(drop=True)


class QryStock:
    """集保個股查詢頁（有 CSRF token，每次回應都會換新 token）。"""

    def __init__(self, min_interval: float = 1.5):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0 (twstock-db-crawler)"
        self.min_interval = min_interval
        self._last = 0.0
        self.token = None
        self.dates: list[str] = []

    def _wait(self):
        w = self.min_interval - (time.monotonic() - self._last)
        if w > 0:
            time.sleep(w)
        self._last = time.monotonic()

    def _absorb(self, html: str):
        m = re.search(r'name="SYNCHRONIZER_TOKEN" value="([^"]+)"', html)
        if m:
            self.token = m.group(1)

    def open(self) -> list[str]:
        """開啟查詢頁，回傳可查詢的週日期（YYYYMMDD，新到舊）。"""
        self._wait()
        r = self.s.get(QRY_URL, timeout=60)
        r.raise_for_status()
        self._absorb(r.text)
        sel = re.search(r'<select name="scaDate".*?</select>', r.text, re.S)
        self.dates = re.findall(r'value="(\d{8})"', sel.group(0) if sel else "")
        return self.dates

    def query(self, stock_id: str, yyyymmdd: str) -> pd.DataFrame | None:
        """回傳該檔該週的分級資料；查無資料回傳空表。"""
        if not self.token:
            self.open()
        self._wait()
        data = {"SYNCHRONIZER_TOKEN": self.token, "SYNCHRONIZER_URI": "/portal/zh/smWeb/qryStock",
                "method": "submit", "firDate": self.dates[0] if self.dates else yyyymmdd,
                "scaDate": yyyymmdd, "sqlMethod": "StockNo", "stockNo": stock_id, "stockName": ""}
        r = self.s.post(QRY_URL, data=data, timeout=60)
        r.raise_for_status()
        self._absorb(r.text)
        try:
            tables = pd.read_html(io.StringIO(r.text))
        except ValueError:
            return pd.DataFrame(columns=COLS)
        tbl = next((t for t in tables if t.shape[1] == 5 and "序" in str(t.columns[0])), None)
        if tbl is None or tbl.empty:
            return pd.DataFrame(columns=COLS)
        tbl.columns = ["seq", "range", "people", "shares", "percent"]
        out = pd.DataFrame({
            "date": f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}",
            "stock_id": stock_id,
            "level": tbl["range"].map(range_to_level),
            "people": tbl["people"].map(to_num),
            "shares": tbl["shares"].map(to_num),
            "percent": tbl["percent"].map(to_num),
        })
        out = out[out["level"].notna()]
        if (out["shares"].fillna(0) == 0).all():
            return pd.DataFrame(columns=COLS)
        return out.astype({c: "float64" for c in ["level", "people", "shares", "percent"]})


def major_holder_ratio(df: pd.DataFrame, min_level: int = 12) -> pd.DataFrame:
    """400 張以上大戶持股比例（%）。回傳 date, stock_id, ratio。"""
    if df.empty:
        return pd.DataFrame(columns=["date", "stock_id", "ratio"])
    sub = df[(df["level"] >= min_level) & (df["level"] <= 15)]
    return sub.groupby(["date", "stock_id"], as_index=False)["percent"].sum().rename(
        columns={"percent": "ratio"})


if __name__ == "__main__":  # 手動測試
    q = QryStock()
    print(q.open()[:3])
    log.info("%s", q.query("2330", q.dates[0]))
