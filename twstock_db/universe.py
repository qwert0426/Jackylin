"""股票清單（上市、上櫃、興櫃、ETF、已下市）。

來源：FinMind TaiwanStockInfo / TaiwanStockDelisting，加上官方每日行情出現過的代號。
結果存成 meta/stock_info.parquet：
  stock_id, stock_name, market(上市/上櫃/興櫃), kind(stock/etf), industry,
  status(上市中/已下市), delist_date
"""
from __future__ import annotations

import pandas as pd

from .config import KIND_ETF, KIND_STOCK, MKT_EMERGING, MKT_LISTED, MKT_OTC, Settings
from .sources.finmind import FinMind
from .util import classify_id, iso_now, log, today_tw

COLS = ["stock_id", "stock_name", "market", "kind", "industry", "status", "delist_date",
        "updated_at"]


def _market(t: str) -> str | None:
    t = str(t).lower()
    if "emerging" in t:
        return MKT_EMERGING
    if "tpex" in t or "otc" in t:
        return MKT_OTC
    if "twse" in t:
        return MKT_LISTED
    return None


def refresh_from_finmind(fm: FinMind, old: pd.DataFrame) -> pd.DataFrame:
    info = fm.get("TaiwanStockInfo", start_date="1990-01-01")
    if info.empty:
        raise RuntimeError("TaiwanStockInfo 回傳空資料")
    info["stock_id"] = info["stock_id"].astype(str).str.strip()
    info["kind"] = info["stock_id"].map(classify_id)
    info["market"] = info["type"].map(_market)
    info = info[info["kind"].notna() & info["market"].notna()]
    bad = info["industry_category"].astype(str).str.contains("Index|大盤|指數", regex=True)
    info = info[~bad]
    info = info.sort_values("date").groupby("stock_id").agg(
        stock_name=("stock_name", "last"), market=("market", "last"), kind=("kind", "last"),
        industry=("industry_category", lambda s: "、".join(sorted(set(map(str, s)))))).reset_index()
    info["status"] = "上市中"
    info["delist_date"] = ""

    try:
        dl = fm.get("TaiwanStockDelisting", start_date="1990-01-01")
    except Exception as e:  # noqa: BLE001
        log.warning("下市清單抓取失敗：%s", e)
        dl = pd.DataFrame()
    if not dl.empty:
        dl["stock_id"] = dl["stock_id"].astype(str).str.strip()
        dl["kind"] = dl["stock_id"].map(classify_id)
        dl = dl[dl["kind"].notna()].sort_values("date").groupby("stock_id").last().reset_index()
        dl = dl[~dl["stock_id"].isin(info["stock_id"])]
        # 已下市股票不知道原本市場時，沿用舊紀錄
        prev_mkt = dict(zip(old.get("stock_id", []), old.get("market", [])))
        dl_rows = pd.DataFrame({
            "stock_id": dl["stock_id"], "stock_name": dl.get("stock_name", ""),
            "market": dl["stock_id"].map(lambda s: prev_mkt.get(s) or MKT_LISTED),
            "kind": dl["kind"], "industry": "", "status": "已下市",
            "delist_date": pd.to_datetime(dl["date"]).dt.strftime("%Y-%m-%d"),
        })
        info = pd.concat([info, dl_rows], ignore_index=True)
    info["updated_at"] = iso_now()
    return info[COLS]


def filter_universe(info: pd.DataFrame, s: Settings) -> pd.DataFrame:
    """依設定檔的股票範圍篩選。"""
    if info.empty:
        return info
    u = s.universe or {}
    keep = pd.Series(True, index=info.index)
    keep &= ~((info["market"] == MKT_LISTED) & (info["kind"] == KIND_STOCK)) | bool(u.get("listed", True))
    keep &= ~((info["market"] == MKT_OTC) & (info["kind"] == KIND_STOCK)) | bool(u.get("otc", True))
    keep &= ~(info["market"] == MKT_EMERGING) | bool(u.get("emerging", True))
    keep &= ~(info["kind"] == KIND_ETF) | bool(u.get("etf", True))
    keep &= ~(info["status"] == "已下市") | bool(u.get("delisted", True))
    # 在起始日之前就下市的不收
    keep &= ~((info["status"] == "已下市") & (info["delist_date"] < s.start_date))
    return info[keep].reset_index(drop=True)


def needs_refresh(info: pd.DataFrame) -> bool:
    if info.empty or "updated_at" not in info:
        return True
    return str(info["updated_at"].max())[:10] < today_tw().isoformat()
