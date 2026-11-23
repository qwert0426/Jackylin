"""集保股權分散表（大戶持股）：每週全市場快照 + 過去一年逐檔回補。"""
from __future__ import annotations

import pandas as pd

from .config import Settings
from .sources import tdcc
from .sources.finmind import FinMind, LevelRequired, QuotaExceeded
from .storage.store import Store
from .util import Deadline, iso_now, log

DONE_COLS = ["date", "stock_id", "status", "fetched_at"]


def _finmind_level(s: str) -> float:
    lv = tdcc.range_to_level(s)
    return 16.0 if lv != lv else lv


def run_holding(store: Store, info: pd.DataFrame, s: Settings, deadline: Deadline,
                fm: FinMind | None = None) -> dict:
    summary = {"weekly_date": None, "backfill_queries": 0, "rows": 0, "errors": []}
    if not s.enabled("holding"):
        return summary
    done = store.read_meta("tdcc_done")
    if done.empty:
        done = pd.DataFrame(columns=DONE_COLS)
    have = set(zip(done["date"], done["stock_id"]))
    new: list[dict] = []

    # 1) 每週全市場快照（一次下載）
    try:
        wk = tdcc.latest_week()
        if not wk.empty:
            d = wk["date"].iloc[0]
            if not ((done["date"] == d).sum() > 100):
                store.upsert("holding", wk)
                ids = wk["stock_id"].unique()
                new += [{"date": d, "stock_id": i, "status": "ok", "fetched_at": iso_now()} for i in ids]
                have |= {(d, i) for i in ids}
                summary["rows"] += len(wk)
                log.info("集保每週快照 %s：%d 檔", d, len(ids))
            summary["weekly_date"] = d
    except Exception as e:  # noqa: BLE001
        log.warning("集保開放資料下載失敗：%s", e)
        summary["errors"].append(f"tdcc opendata: {e}")

    targets = info[info["kind"].isin(["stock", "etf"]) & (info["status"] == "上市中")].copy()
    targets["_p"] = targets["market"].map({"上市": 0, "上櫃": 1, "興櫃": 3}).fillna(4) + \
        (targets["kind"] == "etf") * 2
    targets = targets.sort_values(["_p", "stock_id"])

    # 2a) 付費會員：FinMind 補齊 2010 年以來
    if s.finmind_sponsor and fm is not None:
        prog = store.read_meta("holding_finmind")
        got = set(prog["stock_id"]) if not prog.empty else set()
        rows = []
        for sid in targets["stock_id"]:
            if sid in got or deadline.expired(30):
                continue
            try:
                df = fm.get("TaiwanStockHoldingSharesPer", sid, start_date=s.start_date)
            except (QuotaExceeded, LevelRequired) as e:
                log.warning("FinMind 大戶持股停止：%s", e)
                break
            except Exception as e:  # noqa: BLE001
                summary["errors"].append(f"finmind holding {sid}: {e}")
                continue
            if not df.empty:
                out = pd.DataFrame({"date": df["date"], "stock_id": sid,
                                    "level": df["HoldingSharesLevel"].map(_finmind_level),
                                    "people": df["people"], "shares": df["unit"],
                                    "percent": df["percent"]})
                store.upsert("holding", out)
                summary["rows"] += len(out)
            rows.append({"stock_id": sid, "fetched_at": iso_now()})
        if rows:
            store.write_meta("holding_finmind", pd.concat([prog, pd.DataFrame(rows)], ignore_index=True))

    # 2b) 免費：集保個股查詢頁，回補最近約一年（一檔一檔、一週一週）
    q = tdcc.QryStock()
    try:
        dates = q.open()
    except Exception as e:  # noqa: BLE001
        summary["errors"].append(f"tdcc qryStock: {e}")
        dates = []
    sub_deadline = Deadline(s.budget("tdcc_backfill", 12))
    stop = False
    for sid in targets["stock_id"]:
        for ymd in reversed(dates):  # 舊的先補（最舊的週會最先從集保網站下架）
            dstr = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
            if (dstr, sid) in have:
                continue
            if sub_deadline.expired() or deadline.expired(60):
                stop = True
                break
            try:
                df = q.query(sid, ymd)
                summary["backfill_queries"] += 1
            except Exception as e:  # noqa: BLE001
                summary["errors"].append(f"tdcc {sid} {ymd}: {e}")
                q.token = None
                continue
            status = "ok" if df is not None and not df.empty else "nodata"
            if status == "ok":
                store.upsert("holding", df)
                summary["rows"] += len(df)
            new.append({"date": dstr, "stock_id": sid, "status": status, "fetched_at": iso_now()})
            have.add((dstr, sid))
        if stop:
            break
    if new:
        done = pd.concat([done, pd.DataFrame(new)], ignore_index=True)
        store.write_meta("tdcc_done", done.drop_duplicates(["date", "stock_id"], keep="last"))
    log.info("集保回補：查詢 %d 次", summary["backfill_queries"])
    return summary
