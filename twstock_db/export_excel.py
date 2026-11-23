"""個股 Excel（給手機 / 電腦直接看）：每檔一個檔，多個分頁，最新資料在最上面。

每次執行輪流更新一批（資料有變動、或最久沒更新的優先）。
"""
from __future__ import annotations

import re

import pandas as pd

from . import derived
from .sources.tdcc import major_holder_ratio
from .storage.store import Store
from .util import Deadline, iso_now, log

STATE_COLS = ["stock_id", "path", "exported_at", "signature"]

SHEETS = [
    ("日K", "price", {"date": "日期", "open": "開盤", "max": "最高", "min": "最低", "close": "收盤",
                     "spread": "漲跌", "Trading_Volume": "成交股數", "Trading_money": "成交金額",
                     "Trading_turnover": "成交筆數", "adj_close": "還原收盤", "adj_open": "還原開盤",
                     "adj_max": "還原最高", "adj_min": "還原最低", "adj_factor": "還原因子"}),
    ("估值", "per", {"date": "日期", "PER": "本益比", "PBR": "股價淨值比", "dividend_yield": "殖利率(%)"}),
    ("三大法人", "institutional", {"date": "日期", "外資": "外資買賣超(股)", "投信": "投信買賣超(股)",
                               "自營商": "自營商買賣超(股)", "三大法人合計": "三大法人合計(股)"}),
    ("融資融券", "margin", {"date": "日期", "MarginPurchaseTodayBalance": "融資餘額(張)",
                         "MarginPurchaseBuy": "融資買進", "MarginPurchaseSell": "融資賣出",
                         "ShortSaleTodayBalance": "融券餘額(張)", "ShortSaleSell": "融券賣出",
                         "ShortSaleBuy": "融券買進", "OffsetLoanAndShort": "資券互抵"}),
    ("外資持股", "foreign_holding", {"date": "日期", "ForeignInvestmentSharesRatio": "外資持股比例(%)",
                                 "ForeignInvestmentShares": "外資持股股數",
                                 "NumberOfSharesIssued": "發行股數"}),
    ("借券", "short_balance", {"date": "日期", "SBLShortSalesCurrentDayBalance": "借券賣出餘額(股)",
                             "SBLShortSalesShortSales": "借券當日賣出", "SBLShortSalesReturns": "借券當日還券",
                             "MarginShortSalesCurrentDayBalance": "融券餘額(股)"}),
    ("當沖", "day_trading", {"date": "日期", "Volume": "當沖成交股數", "BuyAmount": "當沖買進金額",
                            "SellAmount": "當沖賣出金額", "BuyAfterSale": "暫停先賣後買註記"}),
    ("大戶持股", "holding", {}),
    ("月營收", "month_revenue", {"date": "公布月份", "revenue_year": "營收年", "revenue_month": "營收月",
                              "revenue": "營收", "月增率(%)": "月增率(%)", "年增率(%)": "年增率(%)"}),
    ("財報季", "income", {}),
    ("除權息", "dividend", {"date": "除權息日", "before_price": "除權息前收盤", "after_price": "參考價",
                          "stock_and_cache_dividend": "權值+息值", "stock_or_cache_dividend": "權/息"}),
]

NEEDED = ["price", "per", "institutional", "margin", "foreign_holding", "short_balance",
          "day_trading", "holding", "month_revenue", "income", "balance", "dividend",
          "capital_reduction"]


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "", str(name)) or "未命名"


def _path(r) -> str:
    folder = "已下市" if r.status == "已下市" else r.market
    return f"個股Excel/{folder}/{r.stock_id}_{_safe(r.stock_name)}.xlsx"


def _signature(store: Store, sid: str) -> str:
    cov = store.coverage()
    c = cov[cov["stock_id"] == sid]
    if c.empty:
        return ""
    return f"{int(c['rows'].sum())}|{c['max_date'].max()}"


def run_export(store: Store, info: pd.DataFrame, per_run: int, deadline: Deadline) -> dict:
    state = store.read_meta("export_state")
    if state.empty:
        state = pd.DataFrame(columns=STATE_COLS)
    st = state.set_index("stock_id") if not state.empty else state
    cand = []
    for r in info.itertuples(index=False):
        sig = _signature(store, r.stock_id)
        if not sig:
            continue
        prev = st.loc[r.stock_id] if (not st.empty and r.stock_id in st.index) else None
        if prev is not None and prev["signature"] == sig and prev["path"] == _path(r):
            continue
        cand.append((prev["exported_at"] if prev is not None else "", r))
    cand.sort(key=lambda x: (x[0], x[1].stock_id))
    batch = [r for _, r in cand[:per_run]]
    summary = {"exported": 0, "pending": len(cand)}
    if not batch:
        return summary
    ids = [r.stock_id for r in batch]
    log.info("匯出個股 Excel：本批 %d 檔（尚待更新 %d 檔）", len(batch), len(cand))
    data = {ds: store.load(ds, stock_ids=ids) for ds in NEEDED}
    events = derived.adjust_factors(data["dividend"], data["capital_reduction"])
    data["price"] = derived.adjusted_prices(data["price"], events)
    data["institutional"] = derived.institutional_wide(data["institutional"])
    data["month_revenue"] = derived.revenue_growth(data["month_revenue"])
    ratios = derived.quarterly_ratios(data["income"], data["balance"])
    hold = data["holding"]
    if not hold.empty:
        big = major_holder_ratio(hold).rename(columns={"ratio": "400張以上大戶持股(%)"})
        k1000 = major_holder_ratio(hold, 15).rename(columns={"ratio": "1000張以上大戶持股(%)"})
        tot = hold[hold["level"] == 17][["date", "stock_id", "people"]].rename(columns={"people": "總股東人數"})
        data["holding"] = big.merge(k1000, on=["date", "stock_id"], how="outer").merge(
            tot, on=["date", "stock_id"], how="left")
    by = {ds: ({k: g for k, g in df.groupby("stock_id")} if not df.empty and "stock_id" in df else {})
          for ds, df in data.items()}
    by["ratios"] = {k: g for k, g in ratios.groupby("stock_id")} if not ratios.empty else {}
    new_state = []
    for r in batch:
        if deadline.expired(30):
            break
        rel = _path(r)
        p = store.local(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        with pd.ExcelWriter(p, engine="openpyxl") as xw:
            pd.DataFrame({"項目": ["代號", "名稱", "市場", "類型", "產業", "狀態", "下市日期", "匯出時間"],
                          "內容": [r.stock_id, r.stock_name, r.market,
                                 "ETF" if r.kind == "etf" else "股票", r.industry, r.status,
                                 r.delist_date, iso_now()]}).to_excel(xw, sheet_name="基本資料", index=False)
            for sheet, ds, cols in SHEETS:
                src = "ratios" if ds == "income" else ds
                df = by.get(src, {}).get(r.stock_id)
                if df is None or df.empty:
                    continue
                df = df.drop(columns=[c for c in ["stock_id"] if c in df])
                if cols:
                    df = df[[c for c in cols if c in df]].rename(columns=cols)
                elif "date" in df:
                    df = df.rename(columns={"date": "日期"})
                key = df.columns[0]
                df.sort_values(key, ascending=False).to_excel(xw, sheet_name=sheet, index=False)
        store.mark_dirty(rel)
        old = st.loc[r.stock_id]["path"] if (not st.empty and r.stock_id in st.index) else None
        if old and old != rel:
            store.backend.delete(old)
            store.remote.pop(old, None)
        new_state.append({"stock_id": r.stock_id, "path": rel, "exported_at": iso_now(),
                          "signature": _signature(store, r.stock_id)})
        summary["exported"] += 1
    if new_state:
        upd = pd.DataFrame(new_state)
        state = state[~state["stock_id"].isin(upd["stock_id"])] if not state.empty else state
        store.write_meta("export_state", pd.concat([state, upd], ignore_index=True))
    summary["pending"] -= summary["exported"]
    return summary
