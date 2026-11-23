"""每日增量：用證交所 / 櫃買中心全市場 API 抓最近 30 天還沒抓到的交易日。

更早的缺口，由 FinMind 逐檔補（planner.py）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from .config import Settings
from .sources import official
from .storage.store import Store
from .util import Deadline, log, now_tw

LOG_COLS = ["source", "date", "status", "rows", "fetched_at", "error"]
FINAL = {"ok", "holiday", "empty"}
CATCHUP_DAYS = 30


def _weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def run_daily(store: Store, s: Settings, deadline: Deadline) -> dict:
    now = now_tw()
    today = now.date()
    hour = now.hour + now.minute / 60
    olog = store.read_meta("official_log")
    if olog.empty:
        olog = pd.DataFrame(columns=LOG_COLS)
    state = {(r.source, r.date): r for r in olog.itertuples(index=False)}
    new_rows: dict[tuple, dict] = {}
    start = max(today - timedelta(days=CATCHUP_DAYS),
                datetime.strptime(s.start_date, "%Y-%m-%d").date())
    summary = {"requests": 0, "rows": 0, "dates": set(), "errors": []}

    def status(src, ds_):
        key = (src, ds_)
        if key in new_rows:
            return new_rows[key]["status"]
        r = state.get(key)
        return r.status if r is not None else None

    def record(src, ds_, st, rows=0, err=""):
        new_rows[(src, ds_)] = {"source": src, "date": ds_, "status": st, "rows": rows,
                                "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"), "error": err[:300]}

    for d in _weekdays(start, today):
        dstr = d.isoformat()
        if status("twse_price", dstr) == "holiday":
            continue
        for src, ds, fn, _mkt, pub in official.SOURCES:
            if not s.enabled(ds) or status(src, dstr) in FINAL:
                continue
            if d == today and hour < pub:
                continue
            prev = new_rows.get((src, dstr)) or (state.get((src, dstr))._asdict()
                                                  if (src, dstr) in state else None)
            if prev and prev["fetched_at"] >= (now - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S"):
                continue
            client = official.TWSE if src.startswith("twse") else official.TPEX
            if client.blocked or deadline.expired(30):
                continue
            try:
                df = fn(d)
                summary["requests"] += 1
            except official.Blocked:
                log.warning("%s 暫時封鎖了請求，本次略過該網站", src.split("_")[0].upper())
                summary["errors"].append(f"{src} {dstr} blocked")
                continue
            except official.FormatChanged as e:
                log.error("%s %s 格式改變：%s", src, dstr, e)
                record(src, dstr, "empty", 0, f"格式改變：{e}")
                summary["errors"].append(f"{src} {dstr} 格式改變")
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("%s %s 失敗：%s", src, dstr, e)
                record(src, dstr, "error", 0, str(e))
                summary["errors"].append(f"{src} {dstr} {e}")
                continue
            if df is not None and not df.empty:
                store.upsert(ds, df)
                record(src, dstr, "ok", len(df))
                summary["rows"] += len(df)
                summary["dates"].add(dstr)
                log.info("官方 %s %s：%d 筆", src, dstr, len(df))
            elif src == "twse_price" and (d < today or hour >= 18):
                record(src, dstr, "holiday")
                break
            elif np.busday_count(d, today) >= 3 and status("twse_price", dstr) == "ok":
                record(src, dstr, "empty", 0, "交易日但來源無資料，改由 FinMind 補")
            else:
                record(src, dstr, "pending")

    if new_rows:
        upd = pd.DataFrame(list(new_rows.values()))
        keep = olog[~olog.set_index(["source", "date"]).index.isin(list(new_rows))] if not olog.empty else olog
        olog = pd.concat([keep, upd], ignore_index=True)
        cutoff = (today - timedelta(days=400)).isoformat()
        olog = olog[olog["date"] >= cutoff].sort_values(["date", "source"])
        store.write_meta("official_log", olog)
    summary["dates"] = sorted(summary["dates"])
    return summary


def latest_trading_date(store: Store) -> str | None:
    olog = store.read_meta("official_log")
    if not olog.empty:
        ok = olog[(olog["source"] == "twse_price") & (olog["status"] == "ok")]
        if not ok.empty:
            return str(ok["date"].max())
    cov = store.coverage()
    cov = cov[cov["dataset"] == "price"] if not cov.empty else cov
    return str(cov["max_date"].max()) if not cov.empty else None
