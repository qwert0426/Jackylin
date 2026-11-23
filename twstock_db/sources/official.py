"""證交所（TWSE）與櫃買中心（TPEx）官方每日全市場資料。

一次請求 = 某一天整個市場的一種資料，所以拿來做每日增量更新最省。
輸出欄位統一成 FinMind 的欄位名稱，方便與歷史資料合併。
"""
from __future__ import annotations

import time
from datetime import date

import numpy as np
import pandas as pd
import requests

from ..util import classify_id, log, roc_to_iso, to_num

UA = {"User-Agent": "Mozilla/5.0 (twstock-db-crawler)", "Accept": "application/json"}


class Blocked(Exception):
    """被來源網站暫時封鎖（請求太頻繁）。"""


class FormatChanged(Exception):
    """來源欄位格式和預期不同。"""


class _Client:
    def __init__(self, min_interval: float):
        self.s = requests.Session()
        self.s.headers.update(UA)
        self.min_interval = min_interval
        self._last = 0.0
        self.blocked = False

    def get_json(self, url: str, params: dict) -> dict:
        if self.blocked:
            raise Blocked(url)
        for attempt in range(3):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.s.get(url, params=params, timeout=60, allow_redirects=False)
            except requests.RequestException as e:
                log.warning("連線錯誤 %s：%s", url, e)
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code in (301, 302, 307, 403) or "FOR SECURITY REASONS" in r.text[:2000]:
                self.blocked = True
                raise Blocked(url)
            try:
                return r.json()
            except ValueError:
                log.warning("非 JSON 回應 %s：%s", url, r.text[:120])
                time.sleep(10 * (attempt + 1))
        raise RuntimeError(f"{url} 重試失敗")


TWSE = _Client(min_interval=4.0)   # 證交所請求太密會被封鎖，間隔 4 秒
TPEX = _Client(min_interval=2.0)


def _find_table(d: dict, must_field: str, index: int | None = None) -> dict | None:
    tables = d.get("tables")
    if tables is None and d.get("fields"):
        tables = [{"fields": d.get("fields"), "data": d.get("data")}]
    hits = [t for t in (tables or []) if t.get("fields") and must_field in t["fields"]]
    if not hits:
        return None
    return hits[index or 0]


def _rows(table: dict | None) -> list:
    return list((table or {}).get("data") or [])


def _keep(rows: list, id_idx: int) -> list:
    return [r for r in rows if classify_id(str(r[id_idx]).strip())]


def _check(table: dict, n_fields: int, name: str) -> None:
    if len(table["fields"]) != n_fields:
        raise FormatChanged(f"{name} 欄位數 {len(table['fields'])} ≠ {n_fields}：{table['fields']}")


def _num_cols(df: pd.DataFrame, skip: set) -> pd.DataFrame:
    for c in df.columns:
        if c not in skip:
            df[c] = df[c].map(to_num).astype("float64")
    return df


def _frame(rows: list, mapping: dict, d: date, text: set = frozenset()) -> pd.DataFrame:
    out = {col: [str(r[i]).strip() if col in text or col == "stock_id" else r[i] for r in rows]
           for col, i in mapping.items()}
    df = pd.DataFrame(out)
    df.insert(0, "date", d.isoformat())
    return _num_cols(df, {"date", "stock_id"} | set(text))


def _twse_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def _tpex_date(d: date) -> str:
    return d.strftime("%Y/%m/%d")


def _twse_ok(d: dict) -> bool:
    return str(d.get("stat", "")).upper() == "OK"


# ---------------------------------------------------------------- 日K
def twse_price(d: date) -> pd.DataFrame | None:
    j = TWSE.get_json("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                      {"date": _twse_date(d), "type": "ALLBUT0999", "response": "json"})
    if not _twse_ok(j):
        return None
    t = _find_table(j, "證券代號")
    if not t or not t.get("data"):
        return None
    f = t["fields"]
    ix = {k: f.index(k) for k in ["證券代號", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價",
                                  "最低價", "收盤價", "漲跌(+/-)", "漲跌價差"]}
    rows = _keep(t["data"], ix["證券代號"])
    recs = []
    for r in rows:
        if to_num(r[ix["開盤價"]]) != to_num(r[ix["開盤價"]]):  # 無成交（NaN）
            continue
        sign = -1.0 if "-" in str(r[ix["漲跌(+/-)"]]) else 1.0
        diff = to_num(r[ix["漲跌價差"]])
        recs.append({
            "date": d.isoformat(), "stock_id": str(r[ix["證券代號"]]).strip(),
            "Trading_Volume": to_num(r[ix["成交股數"]]), "Trading_money": to_num(r[ix["成交金額"]]),
            "open": to_num(r[ix["開盤價"]]), "max": to_num(r[ix["最高價"]]),
            "min": to_num(r[ix["最低價"]]), "close": to_num(r[ix["收盤價"]]),
            "spread": 0.0 if np.isnan(diff) else sign * diff,
            "Trading_turnover": to_num(r[ix["成交筆數"]]),
        })
    return pd.DataFrame(recs)


def tpex_price(d: date) -> pd.DataFrame | None:
    j = TPEX.get_json("https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes",
                      {"date": _tpex_date(d), "response": "json"})
    tables = [t for t in (j.get("tables") or []) if t.get("fields") and "代號" in t["fields"]]
    rows = [r for t in tables for r in (t.get("data") or [])]
    if not rows:
        return None
    f = tables[0]["fields"]
    ix = {k: f.index(k) for k in ["代號", "收盤", "漲跌", "開盤", "最高", "最低", "成交股數",
                                  "成交金額(元)", "成交筆數"]}
    vol_mul = 1000.0 if "千股" in str(j.get("flagField", "")) else 1.0
    recs = []
    for r in _keep(rows, ix["代號"]):
        o = to_num(r[ix["開盤"]])
        if np.isnan(o) or o == 0:
            continue
        sp = to_num(r[ix["漲跌"]])
        recs.append({
            "date": d.isoformat(), "stock_id": str(r[ix["代號"]]).strip(),
            "Trading_Volume": to_num(r[ix["成交股數"]]) * vol_mul,
            "Trading_money": to_num(r[ix["成交金額(元)"]]),
            "open": o, "max": to_num(r[ix["最高"]]), "min": to_num(r[ix["最低"]]),
            "close": to_num(r[ix["收盤"]]), "spread": 0.0 if np.isnan(sp) else sp,
            "Trading_turnover": to_num(r[ix["成交筆數"]]),
        })
    return pd.DataFrame(recs)


# ---------------------------------------------------------------- 本益比
def twse_per(d: date) -> pd.DataFrame | None:
    j = TWSE.get_json("https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d",
                      {"date": _twse_date(d), "selectType": "ALL", "response": "json"})
    if not _twse_ok(j):
        return None
    t = _find_table(j, "證券代號")
    if not t or not t.get("data"):
        return None
    f = t["fields"]
    m = {"stock_id": f.index("證券代號"), "dividend_yield": f.index("殖利率(%)"),
         "PER": f.index("本益比"), "PBR": f.index("股價淨值比")}
    return _frame(_keep(t["data"], m["stock_id"]), m, d).fillna(0.0)


def tpex_per(d: date) -> pd.DataFrame | None:
    j = TPEX.get_json("https://www.tpex.org.tw/www/zh-tw/afterTrading/peQryDate",
                      {"date": _tpex_date(d), "response": "json"})
    t = _find_table(j, "股票代號")
    if not t or not t.get("data"):
        return None
    f = t["fields"]
    m = {"stock_id": f.index("股票代號"), "dividend_yield": f.index("殖利率(%)"),
         "PER": f.index("本益比"), "PBR": f.index("股價淨值比")}
    return _frame(_keep(t["data"], m["stock_id"]), m, d).fillna(0.0)


# ---------------------------------------------------------------- 三大法人
_INST_NAMES = ["Foreign_Investor", "Foreign_Dealer_Self", "Investment_Trust", "Dealer_self",
               "Dealer_Hedging"]


def _inst_long(rows: list, d: date, pos: list) -> pd.DataFrame:
    recs = []
    for r in _keep(rows, 0):
        sid = str(r[0]).strip()
        for name, (bi, si) in zip(_INST_NAMES, pos):
            recs.append({"date": d.isoformat(), "stock_id": sid, "name": name,
                         "buy": to_num(r[bi]), "sell": to_num(r[si])})
    return pd.DataFrame(recs)


def twse_institutional(d: date) -> pd.DataFrame | None:
    j = TWSE.get_json("https://www.twse.com.tw/rwd/zh/fund/T86",
                      {"date": _twse_date(d), "selectType": "ALLBUT0999", "response": "json"})
    if not _twse_ok(j):
        return None
    t = _find_table(j, "證券代號")
    if not t or not t.get("data"):
        return None
    _check(t, 19, "T86")
    return _inst_long(t["data"], d, [(2, 3), (5, 6), (8, 9), (12, 13), (15, 16)])


def tpex_institutional(d: date) -> pd.DataFrame | None:
    j = TPEX.get_json("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
                      {"type": "Daily", "sect": "AL", "date": _tpex_date(d), "response": "json"})
    t = _find_table(j, "代號")
    if not t or not t.get("data"):
        return None
    _check(t, 24, "TPEx dailyTrade")
    return _inst_long(t["data"], d, [(2, 3), (5, 6), (11, 12), (14, 15), (17, 18)])


# ---------------------------------------------------------------- 融資融券
_MARGIN_TWSE = {"stock_id": 0, "MarginPurchaseBuy": 2, "MarginPurchaseSell": 3,
                "MarginPurchaseCashRepayment": 4, "MarginPurchaseYesterdayBalance": 5,
                "MarginPurchaseTodayBalance": 6, "MarginPurchaseLimit": 7, "ShortSaleBuy": 8,
                "ShortSaleSell": 9, "ShortSaleCashRepayment": 10, "ShortSaleYesterdayBalance": 11,
                "ShortSaleTodayBalance": 12, "ShortSaleLimit": 13, "OffsetLoanAndShort": 14,
                "Note": 15}
_MARGIN_TPEX = {"stock_id": 0, "MarginPurchaseYesterdayBalance": 2, "MarginPurchaseBuy": 3,
                "MarginPurchaseSell": 4, "MarginPurchaseCashRepayment": 5,
                "MarginPurchaseTodayBalance": 6, "MarginPurchaseLimit": 9,
                "ShortSaleYesterdayBalance": 10, "ShortSaleSell": 11, "ShortSaleBuy": 12,
                "ShortSaleCashRepayment": 13, "ShortSaleTodayBalance": 14, "ShortSaleLimit": 17,
                "OffsetLoanAndShort": 18, "Note": 19}


def twse_margin(d: date) -> pd.DataFrame | None:
    j = TWSE.get_json("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
                      {"date": _twse_date(d), "selectType": "ALL", "response": "json"})
    if not _twse_ok(j):
        return None
    t = _find_table(j, "代號")
    if not t or not t.get("data"):
        return None
    _check(t, 16, "MI_MARGN")
    return _frame(_keep(t["data"], 0), _MARGIN_TWSE, d, {"Note"})


def tpex_margin(d: date) -> pd.DataFrame | None:
    j = TPEX.get_json("https://www.tpex.org.tw/www/zh-tw/margin/balance",
                      {"date": _tpex_date(d), "response": "json"})
    t = _find_table(j, "代號")
    if not t or not t.get("data"):
        return None
    _check(t, 20, "TPEx margin")
    return _frame(_keep(t["data"], 0), _MARGIN_TPEX, d, {"Note"})


# ---------------------------------------------------------------- 外資持股
def twse_foreign_holding(d: date) -> pd.DataFrame | None:
    j = TWSE.get_json("https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS",
                      {"date": _twse_date(d), "selectType": "ALLBUT0999", "response": "json"})
    if not _twse_ok(j):
        return None
    t = _find_table(j, "證券代號")
    if not t or not t.get("data"):
        return None
    _check(t, 12, "MI_QFIIS")
    m = {"stock_id": 0, "stock_name": 1, "InternationalCode": 2, "NumberOfSharesIssued": 3,
         "ForeignInvestmentRemainingShares": 4, "ForeignInvestmentShares": 5,
         "ForeignInvestmentRemainRatio": 6, "ForeignInvestmentSharesRatio": 7,
         "ForeignInvestmentUpperLimitRatio": 8, "ChineseInvestmentUpperLimitRatio": 9,
         "note": 10, "RecentlyDeclareDate": 11}
    df = _frame(_keep(t["data"], 0), m, d,
                {"stock_name", "InternationalCode", "note", "RecentlyDeclareDate"})
    df["RecentlyDeclareDate"] = df["RecentlyDeclareDate"].map(roc_to_iso)
    return df


def tpex_foreign_holding(d: date) -> pd.DataFrame | None:
    j = TPEX.get_json("https://www.tpex.org.tw/www/zh-tw/insti/qfii",
                      {"date": _tpex_date(d), "response": "json"})
    t = _find_table(j, "代號")
    if not t or not t.get("data"):
        return None
    _check(t, 10, "TPEx qfii")
    m = {"stock_id": 1, "stock_name": 2, "NumberOfSharesIssued": 3,
         "ForeignInvestmentRemainingShares": 4, "ForeignInvestmentShares": 5,
         "ForeignInvestmentRemainRatio": 6, "ForeignInvestmentSharesRatio": 7,
         "ForeignInvestmentUpperLimitRatio": 8, "note": 9}
    return _frame(_keep(t["data"], 1), m, d, {"stock_name", "note"})


# ---------------------------------------------------------------- 借券賣出餘額
_SBL = {"stock_id": 0, "MarginShortSalesPreviousDayBalance": 2, "MarginShortSalesShortSales": 3,
        "MarginShortSalesShortCovering": 4, "MarginShortSalesStockRedemption": 5,
        "MarginShortSalesCurrentDayBalance": 6, "MarginShortSalesQuota": 7,
        "SBLShortSalesPreviousDayBalance": 8, "SBLShortSalesShortSales": 9,
        "SBLShortSalesReturns": 10, "SBLShortSalesAdjustments": 11,
        "SBLShortSalesCurrentDayBalance": 12, "SBLShortSalesQuota": 13}


def twse_short_balance(d: date) -> pd.DataFrame | None:
    j = TWSE.get_json("https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U",
                      {"date": _twse_date(d), "response": "json"})
    if not _twse_ok(j):
        return None
    t = _find_table(j, "代號")
    if not t or not t.get("data"):
        return None
    _check(t, 15, "TWT93U")
    return _frame(_keep(t["data"], 0), _SBL, d)


def tpex_short_balance(d: date) -> pd.DataFrame | None:
    j = TPEX.get_json("https://www.tpex.org.tw/www/zh-tw/margin/sbl",
                      {"date": _tpex_date(d), "response": "json"})
    t = _find_table(j, "股票代號")
    if not t or not t.get("data"):
        return None
    _check(t, 15, "TPEx sbl")
    return _frame(_keep(t["data"], 0), _SBL, d)


# ---------------------------------------------------------------- 當沖
_DT = {"stock_id": 0, "BuyAfterSale": 2, "Volume": 3, "BuyAmount": 4, "SellAmount": 5}


def twse_day_trading(d: date) -> pd.DataFrame | None:
    j = TWSE.get_json("https://www.twse.com.tw/rwd/zh/dayTrading/TWTB4U",
                      {"date": _twse_date(d), "selectType": "All", "response": "json"})
    if not _twse_ok(j):
        return None
    t = _find_table(j, "證券代號")
    if not t or not t.get("data"):
        return None
    _check(t, 6, "TWTB4U")
    return _frame(_keep(t["data"], 0), _DT, d, {"BuyAfterSale"})


def tpex_day_trading(d: date) -> pd.DataFrame | None:
    j = TPEX.get_json("https://www.tpex.org.tw/www/zh-tw/intraday/stat",
                      {"type": "Daily", "date": _tpex_date(d), "response": "json"})
    t = _find_table(j, "證券代號")
    if not t or not t.get("data"):
        return None
    _check(t, 6, "TPEx intraday stat")
    return _frame(_keep(t["data"], 0), _DT, d, {"BuyAfterSale"})


# (來源代號, 資料集, 函式, 市場, 台灣時間幾點後才會公布)
SOURCES = [
    ("twse_price", "price", twse_price, "上市", 14.5),
    ("tpex_price", "price", tpex_price, "上櫃", 14.5),
    ("twse_institutional", "institutional", twse_institutional, "上市", 16.5),
    ("tpex_institutional", "institutional", tpex_institutional, "上櫃", 16.5),
    ("twse_per", "per", twse_per, "上市", 16.5),
    ("tpex_per", "per", tpex_per, "上櫃", 16.5),
    ("twse_day_trading", "day_trading", twse_day_trading, "上市", 16.5),
    ("tpex_day_trading", "day_trading", tpex_day_trading, "上櫃", 16.5),
    ("twse_foreign_holding", "foreign_holding", twse_foreign_holding, "上市", 17.5),
    ("tpex_foreign_holding", "foreign_holding", tpex_foreign_holding, "上櫃", 17.5),
    ("twse_margin", "margin", twse_margin, "上市", 21.5),
    ("tpex_margin", "margin", tpex_margin, "上櫃", 21.5),
    ("twse_short_balance", "short_balance", twse_short_balance, "上市", 21.5),
    ("tpex_short_balance", "short_balance", tpex_short_balance, "上櫃", 21.5),
]
