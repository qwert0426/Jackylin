"""給選股 / 回測程式用的讀取工具。

資料來源可以是：
  A. 電腦安裝「Google 雲端硬碟」桌面版後的同步資料夾，例如
     G:/我的雲端硬碟/台股資料庫   或   ~/Google Drive/My Drive/台股資料庫
  B. 從 Google Drive 下載整個「台股資料庫」資料夾後解壓縮的位置

範例：
    from twstock_db.reader import Database
    db = Database("G:/我的雲端硬碟/台股資料庫")
    px = db.price(["2330", "2317"], start="2020-01-01", adjusted=True)
    rev = db.load("month_revenue", ["2330"])
    ratios = db.ratios(["2330"])            # EPS、毛利率、ROE
    info = db.stock_info()                  # 全部股票清單（含已下市）
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from . import derived


class Database:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def datasets(self) -> list[str]:
        return sorted(p.name for p in (self.root / "data").iterdir() if p.is_dir())

    def load(self, dataset: str, stock_ids=None, start: str | None = None,
             end: str | None = None, columns=None) -> pd.DataFrame:
        filters = []
        if stock_ids is not None:
            filters.append(("stock_id", "in", [str(s) for s in stock_ids]))
        if start:
            filters.append(("date", ">=", start))
        if end:
            filters.append(("date", "<=", end))
        parts = []
        for p in sorted((self.root / "data" / dataset).glob("*.parquet")):
            y = p.stem
            if start and y < start[:4] or end and y > end[:4]:
                continue
            parts.append(pq.read_table(p, filters=filters or None, columns=columns).to_pandas())
        parts = [x for x in parts if not x.empty]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    def stock_info(self) -> pd.DataFrame:
        return pd.read_parquet(self.root / "meta" / "stock_info.parquet")

    def price(self, stock_ids=None, start=None, end=None, adjusted: bool = True) -> pd.DataFrame:
        px = self.load("price", stock_ids, start, end)
        if not adjusted or px.empty:
            return px
        ids = px["stock_id"].unique().tolist()
        ev = derived.adjust_factors(self.load("dividend", ids), self.load("capital_reduction", ids))
        return derived.adjusted_prices(px, ev)

    def ratios(self, stock_ids=None) -> pd.DataFrame:
        return derived.quarterly_ratios(self.load("income", stock_ids), self.load("balance", stock_ids))

    def institutional(self, stock_ids=None, start=None, end=None) -> pd.DataFrame:
        return derived.institutional_wide(self.load("institutional", stock_ids, start, end))
