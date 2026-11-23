"""共用小工具：時間、數字解析、紀錄。"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

TW_TZ = timezone(timedelta(hours=8))

log = logging.getLogger("twstock_db")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")


def now_tw() -> datetime:
    return datetime.now(TW_TZ)


def today_tw() -> date:
    return now_tw().date()


def iso_now() -> str:
    return now_tw().strftime("%Y-%m-%d %H:%M:%S")


class Deadline:
    """簡單的時間預算計時器。"""

    def __init__(self, minutes: float):
        self.end = time.monotonic() + minutes * 60

    def left(self) -> float:
        return self.end - time.monotonic()

    def expired(self, margin_s: float = 0) -> bool:
        return self.left() <= margin_s


_NUM_RE = re.compile(r"[,\s%＊*]")


def to_num(v):
    """把 '1,234'、'12.5%'、'--'、'' 這類字串轉成 float；無法轉換回傳 NaN。"""
    if v is None:
        return np.nan
    if isinstance(v, (int, float, np.integer, np.floating)):
        return float(v)
    s = _NUM_RE.sub("", str(v))
    if s in ("", "-", "--", "---", "X", "N/A", "NA", "nan", "除權", "除息", "除權息"):
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


def roc_to_iso(s: str) -> str:
    """'115/04/10' → '2026-04-10'；無法解析回傳原字串。"""
    m = re.match(r"^\s*(\d{2,3})/(\d{1,2})/(\d{1,2})\s*$", str(s or ""))
    if not m:
        return str(s or "").strip()
    y, mo, d = int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}"


STOCK_RE = re.compile(r"^\d{4}$")
ETF_RE = re.compile(r"^00\d{2,4}[A-Z]?$")


def classify_id(stock_id: str) -> str | None:
    """回傳 'stock' / 'etf'，不在收錄範圍（權證、特別股、債券…）回傳 None。"""
    sid = str(stock_id).strip()
    if ETF_RE.match(sid):
        return "etf"
    if STOCK_RE.match(sid) and not sid.startswith("0"):
        return "stock"
    return None


def normalize_frame(df: pd.DataFrame, text_cols: set) -> pd.DataFrame:
    """統一欄位型別：date/stock_id 為字串，其餘非文字欄位轉 float。"""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"].astype(str).str[:10], format="%Y-%m-%d",
                                    errors="coerce").dt.strftime("%Y-%m-%d")
        df = df[df["date"].notna()]
    if "stock_id" in df.columns:
        df["stock_id"] = df["stock_id"].astype(str).str.strip()
    for c in df.columns:
        if c in text_cols:
            df[c] = df[c].astype("string").fillna("").astype(str)
        elif pd.api.types.is_float_dtype(df[c]):
            continue
        elif pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_bool_dtype(df[c]):
            df[c] = df[c].astype("float64")
        else:
            conv = df[c].map(to_num).astype("float64")
            raw = df[c].astype("string").fillna("").str.strip()
            filled = raw.ne("") & raw.ne("nan") & raw.ne("<NA>") & raw.ne("None")
            # 有內容卻轉不成數字的比例 > 20% → 視為文字欄位（例如「權/息」）
            if filled.sum() and (conv.isna() & filled).sum() / filled.sum() > 0.2:
                df[c] = raw.where(filled, "").astype(str)
            else:
                df[c] = conv
    return df


def years_between(a: str, b: str) -> float:
    try:
        return (pd.Timestamp(b) - pd.Timestamp(a)).days / 365.25
    except Exception:
        return 0.0
