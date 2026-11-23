"""FinMind 逐檔排程：先做「定期更新 / 補缺口」，再做「歷史回補」，直到全部補齊。

進度表 meta/progress.parquet：每個（資料集, 股票）一列
  backfilled   是否已從 start_date 起完整抓過一次
  status       ok / nodata / error / level（需付費）
  last_attempt / last_success / rows / error
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from .config import DATASETS, MKT_EMERGING, Settings
from .sources.finmind import FinMind, LevelRequired, QuotaExceeded
from .storage.store import Store
from .util import Deadline, iso_now, log, today_tw

PROG_COLS = ["dataset", "stock_id", "backfilled", "status", "last_attempt", "last_success",
             "rows", "error"]

# 回補優先順序（越前面越先補）
BACKFILL_ORDER = ["price", "month_revenue", "income", "per", "institutional", "dividend",
                  "margin", "balance", "foreign_holding", "cashflow", "short_balance",
                  "day_trading", "capital_reduction"]


@dataclass
class Task:
    priority: float
    dataset: str
    stock_id: str
    start: str
    kind: str  # refresh / backfill


def applicable(info: pd.DataFrame, ds: str) -> pd.DataFrame:
    d = DATASETS[ds]
    return info[info["kind"].isin(d.kinds) & info["market"].isin(d.markets)]


def _days_since(ts: str | None, today: date) -> float:
    if not ts or ts != ts:
        return 1e9
    try:
        return (datetime.combine(today, datetime.min.time())
                - datetime.strptime(str(ts)[:10], "%Y-%m-%d")).days
    except ValueError:
        return 1e9


def _latest_quarter_end(today: date) -> date:
    for m, d in [(12, 31), (9, 30), (6, 30), (3, 31)]:
        q = date(today.year, m, d)
        if q < today:
            return q
    return date(today.year - 1, 12, 31)


def _quarter_deadline(q: date) -> date:
    return {3: date(q.year, 5, 15), 6: date(q.year, 8, 14), 9: date(q.year, 11, 14),
            12: date(q.year + 1, 3, 31)}[q.month]


def build_tasks(store: Store, info: pd.DataFrame, s: Settings, prog: pd.DataFrame,
                latest_trade: str | None) -> list[Task]:
    today = today_tw()
    tasks: list[Task] = []
    pidx = {(r.dataset, r.stock_id): r for r in prog.itertuples(index=False)}
    qend = _latest_quarter_end(today)
    q_stale = 4 if today <= _quarter_deadline(qend) + timedelta(days=3) else 20
    month_target = date(today.year, today.month, 1).isoformat()

    for rank, ds in enumerate(BACKFILL_ORDER):
        if not s.enabled(ds) or DATASETS[ds].finmind is None:
            continue
        spec = DATASETS[ds]
        summ = store.stock_summary(ds).set_index("stock_id")
        for r in applicable(info, ds).itertuples(index=False):
            p = pidx.get((ds, r.stock_id))
            delisted = r.status == "已下市"
            # ---------- 歷史回補
            if p is None or not bool(p.backfilled):
                if p is not None and p.status in ("error", "level") and _days_since(p.last_attempt, today) < 1:
                    continue
                pri = 100 + rank * 10 + (5 if delisted else 0) + (3 if r.market == MKT_EMERGING else 0) \
                    + (2 if r.kind == "etf" else 0)
                tasks.append(Task(pri, ds, r.stock_id, s.start_date, "backfill"))
                continue
            if delisted:
                continue  # 已下市且回補完成，不必再更新
            since = _days_since(p.last_attempt, today)
            if p.status == "nodata":
                if since >= 60:
                    tasks.append(Task(90, ds, r.stock_id, s.start_date, "refresh"))
                continue
            mx = str(summ["max_date"].get(r.stock_id, "")) if r.stock_id in summ.index else ""
            start = mx or s.start_date
            # ---------- 定期更新 / 補缺口
            if spec.official:
                if r.market == MKT_EMERGING:
                    if since >= 1 and (not latest_trade or mx < latest_trade):
                        tasks.append(Task(20, ds, r.stock_id, start, "refresh"))
                elif latest_trade and mx and since >= 3:
                    lag = (pd.Timestamp(latest_trade) - pd.Timestamp(mx)).days
                    if lag > 7:  # 官方每日更新漏掉的缺口
                        tasks.append(Task(10, ds, r.stock_id, start, "refresh"))
            elif spec.freq == "monthly":
                if mx < month_target and since >= (1 if today.day <= 12 else 7):
                    tasks.append(Task(30, ds, r.stock_id, start, "refresh"))
            elif spec.freq == "quarterly":
                if mx < qend.isoformat() and since >= q_stale:
                    st = (pd.Timestamp(mx) - pd.Timedelta(days=120)).strftime("%Y-%m-%d") if mx else s.start_date
                    tasks.append(Task(40, ds, r.stock_id, st, "refresh"))
            elif spec.freq == "event":
                if since >= (14 if ds == "dividend" else 30):
                    st = (pd.Timestamp(mx) - pd.Timedelta(days=30)).strftime("%Y-%m-%d") if mx else s.start_date
                    tasks.append(Task(60, ds, r.stock_id, st, "refresh"))
    tasks.sort(key=lambda t: (t.priority, t.stock_id))
    return tasks


def run_finmind(store: Store, info: pd.DataFrame, s: Settings, deadline: Deadline,
                latest_trade: str | None, fm: FinMind | None = None) -> dict:
    fm = fm or FinMind()
    prog = store.read_meta("progress")
    if prog.empty:
        prog = pd.DataFrame(columns=PROG_COLS)
    tasks = build_tasks(store, info, s, prog, latest_trade)
    remaining = fm.remaining_quota()
    budget = int(s.finmind_max_requests_per_run)
    if remaining is not None:
        budget = min(budget, max(0, remaining - 10))
    log.info("FinMind：待辦 %d 項（回補 %d、更新 %d），本次額度 %d", len(tasks),
             sum(t.kind == "backfill" for t in tasks), sum(t.kind == "refresh" for t in tasks), budget)
    updates: dict[tuple, dict] = {}
    old = {(r.dataset, r.stock_id): r._asdict() for r in prog.itertuples(index=False)}
    summary = {"requests": 0, "rows": 0, "done": 0, "quota_hit": False, "errors": [],
               "pending_backfill": sum(t.kind == "backfill" for t in tasks)}
    level_blocked: set[str] = set()
    for t in tasks:
        if fm.requests_made >= budget or deadline.expired(20):
            break
        if t.dataset in level_blocked:
            continue
        key = (t.dataset, t.stock_id)
        rec = dict(old.get(key) or {"dataset": t.dataset, "stock_id": t.stock_id,
                                    "backfilled": False, "rows": 0, "last_success": "", "error": ""})
        rec["last_attempt"] = iso_now()
        try:
            df = fm.get(DATASETS[t.dataset].finmind, t.stock_id, start_date=t.start)
        except QuotaExceeded:
            log.warning("FinMind 本小時額度已用完，下次執行再繼續")
            summary["quota_hit"] = True
            break
        except LevelRequired as e:
            level_blocked.add(t.dataset)
            rec.update(status="level", error=str(e)[:200])
            updates[key] = rec
            continue
        except Exception as e:  # noqa: BLE001
            rec.update(status="error", error=str(e)[:200])
            updates[key] = rec
            summary["errors"].append(f"{t.dataset} {t.stock_id}: {e}")
            continue
        if not df.empty:
            if "stock_id" not in df.columns:
                df["stock_id"] = t.stock_id
            store.upsert(t.dataset, df)
        rows = len(df)
        if t.kind == "backfill":
            rec["backfilled"] = True
            rec["status"] = "ok" if rows else "nodata"
        else:
            rec["status"] = "ok" if (rows or rec.get("status") == "ok") else "nodata"
        rec.update(rows=int(rec.get("rows") or 0) + rows, last_success=iso_now(), error="")
        updates[key] = rec
        summary["rows"] += rows
        summary["done"] += 1
    summary["requests"] = fm.requests_made
    if updates:
        upd = pd.DataFrame(list(updates.values()))[PROG_COLS]
        if not prog.empty:
            prog = prog[~prog.set_index(["dataset", "stock_id"]).index.isin(list(updates))]
            prog = pd.concat([prog, upd], ignore_index=True)
        else:
            prog = upd
        prog["backfilled"] = prog["backfilled"].astype(bool)
        prog["rows"] = prog["rows"].fillna(0).astype(int)
        for c in ["status", "last_attempt", "last_success", "error"]:
            prog[c] = prog[c].fillna("").astype(str)
        store.write_meta("progress", prog)
    summary["pending_backfill"] -= sum(1 for k, v in updates.items() if v.get("backfilled")
                                       and not (old.get(k) or {}).get("backfilled"))
    return summary
