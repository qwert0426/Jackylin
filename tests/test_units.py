"""單元測試：python -m pytest tests -q"""
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from twstock_db import daily, derived, planner, stats, universe  # noqa: E402
from twstock_db.config import Settings  # noqa: E402
from twstock_db.sources import official, tdcc  # noqa: E402
from twstock_db.storage.backend import LocalBackend  # noqa: E402
from twstock_db.storage.store import Store  # noqa: E402
from twstock_db.util import TW_TZ, classify_id, normalize_frame, roc_to_iso, to_num  # noqa: E402


# ------------------------------------------------------------------ util
@pytest.mark.parametrize("v,exp", [("1,234", 1234.0), ("12.5%", 12.5), ("--", None), ("", None),
                                   (None, None), (" -3.2 ", -3.2), ("除息", None), (7, 7.0),
                                   ("abc", None), ("X", None)])
def test_to_num(v, exp):
    r = to_num(v)
    assert (np.isnan(r) if exp is None else r == exp)


def test_roc_to_iso():
    assert roc_to_iso("115/04/10") == "2026-04-10"
    assert roc_to_iso("99/1/5") == "2010-01-05"
    assert roc_to_iso("") == ""
    assert roc_to_iso("abc") == "abc"


@pytest.mark.parametrize("sid,exp", [("2330", "stock"), ("0050", "etf"), ("00878", "etf"),
                                     ("00400A", "etf"), ("030001", None), ("2881A", None),
                                     ("TAIEX", None), ("", None), ("0123", None), ("911608", None)])
def test_classify(sid, exp):
    assert classify_id(sid) == exp


def test_normalize_text_detection():
    df = pd.DataFrame({"date": ["2024-01-02", "2024-01-03"], "stock_id": [" 2330", "2330"],
                       "num": ["1,000", "2"], "flag": ["息", "權"], "mixed": ["1", ""]})
    out = normalize_frame(df, {"date", "stock_id"})
    assert out["stock_id"].tolist() == ["2330", "2330"]
    assert out["num"].tolist() == [1000.0, 2.0]
    assert out["flag"].tolist() == ["息", "權"]
    assert out["mixed"].iloc[0] == 1.0 and np.isnan(out["mixed"].iloc[1])
    assert normalize_frame(pd.DataFrame(), set()).empty
    bad = normalize_frame(pd.DataFrame({"date": ["not a date", "2024-01-01"], "stock_id": ["1", "2"]}),
                          {"date", "stock_id"})
    assert bad["date"].tolist() == ["2024-01-01"]


def test_range_to_level():
    assert tdcc.range_to_level("1-999") == 1
    assert tdcc.range_to_level("400,001-600,000") == 12
    assert tdcc.range_to_level("1,000,001以上") == 15
    assert tdcc.range_to_level("差異數調整（說明4）") == 16
    assert tdcc.range_to_level("合　計") == 17
    assert np.isnan(tdcc.range_to_level("???"))


def test_major_holder_ratio():
    df = pd.DataFrame({"date": "2026-11-20", "stock_id": "2330", "level": [11, 12, 13, 14, 15, 17],
                       "people": 1, "shares": 1, "percent": [1.0, 1.08, 0.9, 0.77, 84.14, 100]})
    r = tdcc.major_holder_ratio(df)
    assert round(r["ratio"].iloc[0], 2) == 86.89
    assert tdcc.major_holder_ratio(df.iloc[0:0]).empty


# ------------------------------------------------------------------ derived
def test_adjusted_prices():
    px = pd.DataFrame({"stock_id": "1", "date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
                       "open": 10.0, "max": 10.0, "min": 10.0, "close": [110, 110, 100, 100.0]})
    div = pd.DataFrame({"stock_id": ["1"], "date": ["2024-01-04"], "before_price": [110.0],
                        "after_price": [100.0]})
    cr = pd.DataFrame({"stock_id": ["1"], "date": ["2024-01-05"],
                       "ClosingPriceonTheLastTradingDay": [100.0], "PostReductionReferencePrice": [200.0]})
    ev = derived.adjust_factors(div, cr)
    out = derived.adjusted_prices(px, ev)
    assert out["adj_factor"].round(4).tolist() == [1.8182, 1.8182, 2.0, 1.0]
    # 沒有事件：還原價 = 原價
    out2 = derived.adjusted_prices(px, derived.adjust_factors(pd.DataFrame(), None))
    assert (out2["adj_close"] == out2["close"]).all()


def test_quarterly_ratios_roe():
    rows = []
    for y in (2023, 2024):
        for q in ("03-31", "06-30", "09-30", "12-31"):
            for t, v in [("Revenue", 100), ("GrossProfit", 40), ("EPS", 1.5), ("IncomeAfterTaxes", 25)]:
                rows.append({"stock_id": "1", "date": f"{y}-{q}", "type": t, "value": v})
    inc = pd.DataFrame(rows)
    bal = pd.DataFrame({"stock_id": "1", "date": ["2023-12-31", "2024-12-31"], "type": "Equity",
                        "value": [400.0, 600.0]})
    r = derived.quarterly_ratios(inc, bal).set_index("date")
    assert r.loc["2024-12-31", "ROE(年,%)"] == 20.0      # 100 / avg(400,600)
    assert r.loc["2023-12-31", "ROE(年,%)"] == 25.0      # 只有期末權益 400
    assert np.isnan(r.loc["2024-09-30", "ROE(年,%)"])
    assert r.loc["2024-09-30", "毛利率(%)"] == 40.0


def test_revenue_growth():
    rev = pd.DataFrame({"stock_id": "1", "date": ["2024-02-01", "2025-01-01", "2025-02-01"],
                        "revenue": [100.0, 150.0, 120.0], "revenue_month": [1, 12, 1],
                        "revenue_year": [2024, 2024, 2025]})
    g = derived.revenue_growth(rev).set_index("date")
    assert g.loc["2025-02-01", "年增率(%)"] == 20.0
    assert g.loc["2025-02-01", "月增率(%)"] == -20.0


def test_institutional_wide():
    inst = pd.DataFrame({"stock_id": "1", "date": "2024-01-02",
                         "name": ["Foreign_Investor", "Foreign_Dealer_Self", "Investment_Trust",
                                  "Dealer_self", "Dealer_Hedging", "Unknown"],
                         "buy": [10, 1, 5, 2, 3, 99], "sell": [4, 0, 1, 1, 0, 0]})
    w = derived.institutional_wide(inst).iloc[0]
    assert (w["外資"], w["投信"], w["自營商"], w["三大法人合計"]) == (7, 4, 4, 15)


# ------------------------------------------------------------------ store
@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "cache", LocalBackend(tmp_path / "drive"))


def test_store_upsert_dedupe_and_sync(store, tmp_path):
    a = pd.DataFrame({"date": ["2023-12-29", "2024-01-02"], "stock_id": "2330", "close": [1.0, 2.0]})
    b = pd.DataFrame({"date": ["2024-01-02", "2024-01-03"], "stock_id": "2330", "close": [9.0, 3.0]})
    store.upsert("price", a)
    store.upsert("price", b)
    store.upsert("price", pd.DataFrame())
    store.sync()
    df = pd.read_parquet(tmp_path / "drive/data/price/2024.parquet")
    assert df["close"].tolist() == [9.0, 3.0]        # 重複的日期以新資料為準
    s = store.stock_summary("price").iloc[0]
    assert (s["rows"], s["min_date"], s["max_date"]) == (3, "2023-12-29", "2024-01-03")
    # 新的 Store（模擬下一次執行、沒有本機快取）能從雲端讀回
    s2 = Store(tmp_path / "cache2", LocalBackend(tmp_path / "drive"))
    assert len(s2.load("price")) == 3
    assert len(s2.load("price", stock_ids=["9999"])) == 0
    s2.rebuild_coverage()
    assert int(s2.stock_summary("price")["rows"].iloc[0]) == 3


# ------------------------------------------------------------------ universe / planner
def _info():
    return pd.DataFrame([
        {"stock_id": "2330", "stock_name": "台積電", "market": "上市", "kind": "stock", "industry": "",
         "status": "上市中", "delist_date": ""},
        {"stock_id": "0050", "stock_name": "50", "market": "上市", "kind": "etf", "industry": "",
         "status": "上市中", "delist_date": ""},
        {"stock_id": "1260", "stock_name": "富味鄉", "market": "興櫃", "kind": "stock", "industry": "",
         "status": "上市中", "delist_date": ""},
        {"stock_id": "2311", "stock_name": "日月光", "market": "上市", "kind": "stock", "industry": "",
         "status": "已下市", "delist_date": "2019-06-03"},
        {"stock_id": "1111", "stock_name": "x", "market": "上市", "kind": "stock", "industry": "",
         "status": "已下市", "delist_date": "2005-01-01"},
    ])


def test_filter_universe():
    s = Settings()
    u = universe.filter_universe(_info(), s)
    assert set(u["stock_id"]) == {"2330", "0050", "1260", "2311"}
    s.universe = {"etf": False, "emerging": False, "delisted": False}
    assert set(universe.filter_universe(_info(), s)["stock_id"]) == {"2330"}


def test_build_tasks_backfill_applicability(store, monkeypatch):
    monkeypatch.setattr(planner, "today_tw", lambda: date(2026, 11, 23))
    s = Settings()
    info = universe.filter_universe(_info(), s)
    tasks = planner.build_tasks(store, info, s, pd.DataFrame(columns=planner.PROG_COLS), "2026-11-20")
    got = {(t.dataset, t.stock_id) for t in tasks}
    assert ("month_revenue", "0050") not in got          # ETF 沒有月營收
    assert ("institutional", "1260") not in got          # 興櫃沒有三大法人
    assert ("price", "1260") in got and ("price", "2311") in got
    assert all(t.kind == "backfill" and t.start == "2010-01-01" for t in tasks)
    # 價格最優先；同資料集內已下市排後面
    assert tasks[0].dataset == "price"
    pr = [t.stock_id for t in tasks if t.dataset == "price"]
    assert pr.index("2311") > pr.index("2330")
    s.datasets = {"price": False}
    assert not any(t.dataset == "price" for t in planner.build_tasks(
        store, info, s, pd.DataFrame(columns=planner.PROG_COLS), "2026-11-20"))


def _prog(rows):
    base = {"backfilled": True, "status": "ok", "last_success": "", "rows": 1, "error": ""}
    return pd.DataFrame([{**base, **r} for r in rows])[planner.PROG_COLS]


def test_build_tasks_refresh_rules(store, monkeypatch):
    monkeypatch.setattr(planner, "today_tw", lambda: date(2026, 11, 23))
    s = Settings()
    info = universe.filter_universe(_info(), s)
    store.upsert("month_revenue", pd.DataFrame({"date": ["2026-10-01"], "stock_id": ["2330"], "revenue": [1]}))
    store.upsert("price", pd.DataFrame({"date": ["2026-11-01", "2026-11-20"], "stock_id": ["2330", "0050"],
                                        "close": [1, 1]}))
    store.upsert("income", pd.DataFrame({"date": ["2026-06-30"], "stock_id": ["2330"], "type": ["EPS"], "value": [1]}))
    store.flush()
    prog = _prog([
        {"dataset": "month_revenue", "stock_id": "2330", "last_attempt": "2026-11-10 10:00:00"},
        {"dataset": "price", "stock_id": "2330", "last_attempt": "2026-11-01 10:00:00"},
        {"dataset": "price", "stock_id": "0050", "last_attempt": "2026-11-01 10:00:00"},
        {"dataset": "price", "stock_id": "2311", "last_attempt": "2020-01-01 10:00:00"},
        {"dataset": "income", "stock_id": "2330", "last_attempt": "2026-11-01 10:00:00"},
        {"dataset": "dividend", "stock_id": "2330", "last_attempt": "2026-11-20 10:00:00"},
        {"dataset": "per", "stock_id": "2330", "last_attempt": "2026-11-22 10:00:00", "status": "nodata"},
        {"dataset": "margin", "stock_id": "2330", "last_attempt": "2026-01-01 10:00:00", "status": "nodata"},
    ])
    tasks = {(t.dataset, t.stock_id): t for t in planner.build_tasks(store, info, s, prog, "2026-11-20")}
    assert tasks[("month_revenue", "2330")].start == "2026-10-01"     # 缺 11 月公布的營收
    assert tasks[("price", "2330")].priority == 10                    # 官方漏掉的缺口
    assert ("price", "0050") not in tasks                             # 已是最新
    assert ("price", "2311") not in tasks                             # 已下市且已回補
    assert tasks[("income", "2330")].start == "2026-03-02"            # 缺 Q3，往前多抓 120 天
    assert ("dividend", "2330") not in tasks                          # 14 天內抓過
    assert ("per", "2330") not in tasks                               # 無資料，60 天內不重試
    assert tasks[("margin", "2330")].kind == "refresh"                # 無資料超過 60 天，重試


def test_quarter_helpers():
    assert planner._latest_quarter_end(date(2026, 11, 23)) == date(2026, 9, 30)
    assert planner._latest_quarter_end(date(2026, 1, 5)) == date(2025, 12, 31)
    assert planner._latest_quarter_end(date(2026, 3, 31)) == date(2025, 12, 31)
    assert planner._quarter_deadline(date(2025, 12, 31)) == date(2026, 3, 31)


def test_run_finmind_quota_and_progress(store, monkeypatch):
    from fake_finmind import FakeFinMind
    monkeypatch.setattr(planner, "today_tw", lambda: date(2026, 11, 23))
    s = Settings()
    info = universe.filter_universe(_info(), s)
    fm = FakeFinMind(quota_after=5)
    from twstock_db.util import Deadline
    r = planner.run_finmind(store, info, s, Deadline(5), None, fm)
    assert r["quota_hit"] and r["done"] == 5
    prog = store.read_meta("progress")
    assert len(prog) == 5 and prog["backfilled"].all()
    fm2 = FakeFinMind()
    r2 = planner.run_finmind(store, info, s, Deadline(5), None, fm2)
    assert not any((c[0], c[1]) in {(x.dataset, x.stock_id) for x in prog.itertuples()} for c in fm2.calls)


# ------------------------------------------------------------------ daily
def test_daily_holiday_pending_empty(store, monkeypatch):
    now = datetime(2026, 11, 23, 15, 0, tzinfo=TW_TZ)  # 週一下午 3 點
    monkeypatch.setattr(daily, "now_tw", lambda: now)
    monkeypatch.setattr(daily, "CATCHUP_DAYS", 6)
    calls = []

    def fake(src, has):
        def fn(d):
            calls.append((src, d.isoformat()))
            if d.isoformat() in has:
                return pd.DataFrame({"date": [d.isoformat()], "stock_id": ["2330"], "close": [1.0]})
            return None
        return fn
    trading = {"2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23"}
    monkeypatch.setattr(official, "SOURCES", [
        ("twse_price", "price", fake("twse_price", trading - {"2026-11-23"}), "上市", 14.5),
        ("twse_margin", "margin", fake("twse_margin", {"2026-11-19"}), "上市", 21.5),
    ])
    monkeypatch.setattr(official.TWSE, "blocked", False)
    from twstock_db.util import Deadline
    s = Settings()
    r = daily.run_daily(store, s, Deadline(5))
    log = store.read_meta("official_log").set_index(["source", "date"])["status"]
    assert log[("twse_price", "2026-11-17")] == "ok"
    assert log[("twse_margin", "2026-11-17")] == "empty"    # 交易日超過 3 天仍無資料
    assert log[("twse_margin", "2026-11-20")] == "pending"  # 3 天內，之後再試
    assert log[("twse_price", "2026-11-23")] == "pending"   # 今天 15:00 還沒有資料 → 稍後再試
    assert ("twse_margin", "2026-11-23") not in calls        # 21:30 才公布，不必問
    assert r["rows"] == 5
    # 第二次執行：45 分鐘內不重複請求
    calls.clear()
    daily.run_daily(store, s, Deadline(5))
    assert calls == []
    # 晚上 7 點今天仍沒資料 → 判定休市
    monkeypatch.setattr(daily, "now_tw", lambda: datetime(2026, 11, 23, 19, 0, tzinfo=TW_TZ))
    daily.run_daily(store, s, Deadline(5))
    log = store.read_meta("official_log").set_index(["source", "date"])["status"]
    assert log[("twse_price", "2026-11-23")] == "holiday"
    assert daily.latest_trading_date(store) == "2026-11-20"


# ------------------------------------------------------------------ stats
def test_stats_compute(store, tmp_path):
    s = Settings()
    info = universe.filter_universe(_info(), s)
    store.upsert("price", pd.DataFrame({"date": ["2010-01-04", "2026-11-20"], "stock_id": ["2330", "2330"],
                                        "close": [1.0, 2.0]}))
    store.flush()
    st = stats.compute(store, info, s)
    row = next(d for d in st["datasets"] if d["key"] == "price")
    assert (row["covered"], row["applicable"], row["span10"], row["min_date"]) == (1, 4, 1, "2010-01-04")
    p = stats.write_json(st, tmp_path / "site")
    assert p.exists() and "日K價量" in p.read_text(encoding="utf-8")
    stats.write_sheet(store, st)
    x = pd.read_excel(store.local("資料庫統計.xlsx"), sheet_name=None, header=None)
    assert set(x) == {"總覽", "個股覆蓋", "執行紀錄", "資料來源"}
