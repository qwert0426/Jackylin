"""統計介面：資料覆蓋率 → Google 試算表「資料庫統計」+ 網頁儀表板 stats.json。"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DATASETS, Settings
from .planner import applicable
from .storage.store import Store
from .util import iso_now, log, years_between

SHEET_NAME = "資料庫統計"

SOURCES_TABLE = [
    ["日K價量", "證交所 MI_INDEX、櫃買 dailyQuotes（每日）；FinMind TaiwanStockPrice（歷史）",
     "https://www.twse.com.tw/zh/trading/historical/mi-index.html"],
    ["本益比 / 淨值比 / 殖利率", "證交所 BWIBBU_d、櫃買 peQryDate；FinMind TaiwanStockPER",
     "https://www.twse.com.tw/zh/trading/historical/bwibbu-day.html"],
    ["三大法人買賣超", "證交所 T86、櫃買 insti/dailyTrade；FinMind TaiwanStockInstitutionalInvestorsBuySell",
     "https://www.twse.com.tw/zh/trading/foreign/t86.html"],
    ["融資融券餘額", "證交所 MI_MARGN、櫃買 margin/balance；FinMind TaiwanStockMarginPurchaseShortSale",
     "https://www.twse.com.tw/zh/trading/margin/mi-margn.html"],
    ["外資持股比例", "證交所 MI_QFIIS、櫃買 insti/qfii；FinMind TaiwanStockShareholding",
     "https://www.twse.com.tw/zh/trading/foreign/mi-qfiis.html"],
    ["借券賣出餘額", "證交所 TWT93U、櫃買 margin/sbl；FinMind TaiwanDailyShortSaleBalances",
     "https://www.twse.com.tw/zh/trading/margin/twt93u.html"],
    ["當沖成交量 / 金額", "證交所 TWTB4U、櫃買 intraday/stat；FinMind TaiwanStockDayTrading",
     "https://www.twse.com.tw/zh/trading/day-trading/twtb4u.html"],
    ["月營收", "FinMind TaiwanStockMonthRevenue（原始來源：公開資訊觀測站）", "https://finmindtrade.com/"],
    ["綜合損益表 / 資產負債表 / 現金流量表", "FinMind TaiwanStockFinancialStatements / BalanceSheet / "
     "CashFlowsStatement（原始來源：公開資訊觀測站）", "https://finmindtrade.com/"],
    ["除權息結果 / 減資參考價", "FinMind TaiwanStockDividendResult / CapitalReductionReferencePrice",
     "https://finmindtrade.com/"],
    ["集保股權分散（大戶持股）", "集保結算所開放資料（每週全市場）＋ 集保個股查詢（最近約一年）",
     "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"],
    ["股票清單 / 下市清單", "FinMind TaiwanStockInfo / TaiwanStockDelisting", "https://finmindtrade.com/"],
]


def _span_stats(summ: pd.DataFrame) -> tuple:
    if summ.empty:
        return 0, 0, 0.0
    yrs = [years_between(a, b) for a, b in zip(summ["min_date"], summ["max_date"])]
    yrs = np.array(yrs)
    return int((yrs >= 5).sum()), int((yrs >= 10).sum()), round(float(np.median(yrs)), 1)


def _derived_summary(store: Store, ds: str, types: list[str]) -> pd.DataFrame:
    df = store.load(ds, columns=["stock_id", "date", "type"])
    if df.empty:
        return pd.DataFrame(columns=["stock_id", "rows", "min_date", "max_date"])
    df = df[df["type"].isin(types)]
    return df.groupby("stock_id").agg(rows=("date", "size"), min_date=("date", "min"),
                                      max_date=("date", "max")).reset_index()


def compute(store: Store, info: pd.DataFrame, s: Settings) -> dict:
    prog = store.read_meta("progress")
    rows = []
    per_stock = info[["stock_id", "stock_name", "market", "kind", "status"]].copy()
    for name, d in DATASETS.items():
        if not s.enabled(name):
            continue
        app = applicable(info, name)
        if name == "holding":  # 集保只有目前掛牌中的股票
            app = app[app["status"] != "已下市"]
        summ = store.stock_summary(name)
        summ = summ[summ["stock_id"].isin(app["stock_id"])]
        s5, s10, med = _span_stats(summ)
        if d.finmind and not prog.empty:
            p = prog[(prog["dataset"] == name) & prog["stock_id"].isin(app["stock_id"])]
            bdone = int(p["backfilled"].astype(bool).sum())
        else:
            bdone = len(summ)
        rows.append({
            "key": name, "label": d.label, "category": d.category, "applicable": len(app),
            "covered": int((summ["rows"] > 0).sum()) if not summ.empty else 0,
            "min_date": summ["min_date"].min() if not summ.empty else "",
            "max_date": summ["max_date"].max() if not summ.empty else "",
            "span5": s5, "span10": s10, "median_years": med,
            "rows": int(summ["rows"].sum()) if not summ.empty else 0,
            "backfill_done": bdone, "backfill_total": len(app),
            "source": " / ".join(x.split(" (")[0] for x in d.sources),
        })
        m = summ.set_index("stock_id")
        per_stock[f"{d.label}｜筆數"] = per_stock["stock_id"].map(m["rows"]).fillna(0).astype(int)
        per_stock[f"{d.label}｜最新"] = per_stock["stock_id"].map(m["max_date"]).fillna("")

    # 衍生指標
    stocks = applicable(info, "income")
    for key, label, types in [("eps", "EPS（季）", ["EPS"]), ("gross", "毛利率（季）", ["GrossProfit"])]:
        if not s.enabled("income"):
            break
        summ = _derived_summary(store, "income", types)
        summ = summ[summ["stock_id"].isin(stocks["stock_id"])]
        s5, s10, med = _span_stats(summ)
        rows.append({"key": key, "label": label, "category": "基本面", "applicable": len(stocks),
                     "covered": len(summ), "min_date": summ["min_date"].min() if len(summ) else "",
                     "max_date": summ["max_date"].max() if len(summ) else "", "span5": s5,
                     "span10": s10, "median_years": med, "rows": int(summ["rows"].sum()) if len(summ) else 0,
                     "backfill_done": len(summ), "backfill_total": len(stocks),
                     "source": "由綜合損益表計算"})
    hold = store.stock_summary("holding")
    if not hold.empty:
        app = applicable(info, "holding")
        app = app[app["status"] != "已下市"]
        hold = hold[hold["stock_id"].isin(app["stock_id"])]
        s5, s10, med = _span_stats(hold)
        rows.append({"key": "major400", "label": "400 張以上大戶持股", "category": "籌碼面",
                     "applicable": len(app), "covered": len(hold), "min_date": hold["min_date"].min(),
                     "max_date": hold["max_date"].max(), "span5": s5, "span10": s10,
                     "median_years": med, "rows": int(hold["rows"].sum()) // 17,
                     "backfill_done": len(hold), "backfill_total": len(app),
                     "source": "由集保股權分散表計算（level 12~15）"})

    olog = store.read_meta("official_log")
    runs = store.read_meta("run_log")
    return {
        "generated_at": iso_now(),
        "start_date": s.start_date,
        "universe": {
            "total": len(info),
            "by_market": info.groupby("market").size().to_dict() if not info.empty else {},
            "etf": int((info["kind"] == "etf").sum()) if not info.empty else 0,
            "delisted": int((info["status"] == "已下市").sum()) if not info.empty else 0,
        },
        "datasets": rows,
        "per_stock": per_stock,
        "official_recent": (olog.sort_values("date").tail(80).to_dict("records")
                            if not olog.empty else []),
        "runs": runs.tail(50).iloc[::-1].to_dict("records") if not runs.empty else [],
        "drive_url": store.backend.folder_url(),
    }


def _clean(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return "" if (v != v or math.isinf(v)) else float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v if v is not None else ""


def write_json(st: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in st.items() if k != "per_stock"}
    data = json.loads(json.dumps(data, default=_clean, ensure_ascii=False))
    p = out_dir / "stats.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=_clean), encoding="utf-8")
    return p


def _tables(st: dict) -> dict[str, list[list]]:
    hdr = ["資料", "類別", "已收錄檔數", "應收錄檔數", "覆蓋率", "最早", "最新", "跨五年", "跨十年",
           "每檔中位年數", "總筆數", "歷史回補進度", "來源"]
    ov = [hdr]
    for r in st["datasets"]:
        cov = f"{r['covered'] / r['applicable'] * 100:.1f}%" if r["applicable"] else ""
        ov.append([r["label"], r["category"], r["covered"], r["applicable"], cov, r["min_date"],
                   r["max_date"], r["span5"], r["span10"], r["median_years"], r["rows"],
                   f"{r['backfill_done']} / {r['backfill_total']}", r["source"]])
    u = st["universe"]
    ov += [[], ["更新時間", st["generated_at"]], ["起始日", st["start_date"]],
           ["股票總數", u["total"]], ["ETF", u["etf"]], ["已下市", u["delisted"]]]
    ov += [[f"市場：{k}", v] for k, v in u["by_market"].items()]
    ov += [["說明", "已收錄 = 該檔至少有一筆資料；最早/最新 = 該類資料全市場的收錄範圍；"
                  "跨五年/十年 = 資料期間超過 5/10 年的檔數。"]]
    ps = st["per_stock"]
    per = [list(ps.columns)] + ps.astype(object).where(ps.notna(), "").values.tolist()
    runs = st["runs"]
    rcols = list(runs[0].keys()) if runs else ["time"]
    run_tbl = [rcols] + [[r.get(c, "") for c in rcols] for r in runs]
    src = [["資料", "來源 / API", "網址"]] + SOURCES_TABLE
    return {"總覽": ov, "個股覆蓋": per, "執行紀錄": run_tbl, "資料來源": src}


def write_sheet(store: Store, st: dict) -> None:
    tables = _tables(st)
    tables = {k: [[_clean(v) for v in row] for row in t] for k, t in tables.items()}
    be = store.backend
    if be.kind != "gdrive":
        p = store.local(f"{SHEET_NAME}.xlsx")
        with pd.ExcelWriter(p, engine="openpyxl") as xw:
            for name, t in tables.items():
                pd.DataFrame(t).to_excel(xw, sheet_name=name, index=False, header=False)
        store.mark_dirty(f"{SHEET_NAME}.xlsx")
        return
    sid = be.find_or_create_sheet(SHEET_NAME)
    meta = be._retry(lambda: be.sheets.spreadsheets().get(spreadsheetId=sid).execute())
    existing = {sh["properties"]["title"]: sh["properties"]["sheetId"] for sh in meta["sheets"]}
    reqs = [{"addSheet": {"properties": {"title": n}}} for n in tables if n not in existing]
    if reqs:
        be._retry(lambda: be.sheets.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={"requests": reqs}).execute())
        if "Sheet1" in existing or "工作表1" in existing:
            old = existing.get("Sheet1", existing.get("工作表1"))
            be._retry(lambda: be.sheets.spreadsheets().batchUpdate(
                spreadsheetId=sid, body={"requests": [{"deleteSheet": {"sheetId": old}}]}).execute())
        meta = be._retry(lambda: be.sheets.spreadsheets().get(spreadsheetId=sid).execute())
        existing = {sh["properties"]["title"]: sh["properties"]["sheetId"] for sh in meta["sheets"]}
    for name, t in tables.items():
        width = max(len(r) for r in t) if t else 1
        t = [r + [""] * (width - len(r)) for r in t]
        grid = {"updateSheetProperties": {
            "properties": {"sheetId": existing[name],
                           "gridProperties": {"rowCount": max(len(t) + 10, 100),
                                              "columnCount": max(width, 26),
                                              "frozenRowCount": 1}},
            "fields": "gridProperties(rowCount,columnCount,frozenRowCount)"}}
        be._retry(lambda: be.sheets.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={"requests": [grid]}).execute())
        be._retry(lambda: be.sheets.spreadsheets().values().clear(
            spreadsheetId=sid, range=f"'{name}'").execute())
        be._retry(lambda: be.sheets.spreadsheets().values().update(
            spreadsheetId=sid, range=f"'{name}'!A1", valueInputOption="RAW",
            body={"values": t}).execute())
    log.info("已更新 Google 試算表「%s」", SHEET_NAME)
