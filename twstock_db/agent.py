"""爬蟲 agent 主程式：每次執行依序做以下工作，直到時間或額度用完。

 1. 同步：讀取雲端資料庫索引
 2. 股票清單：每天更新一次（上市 / 上櫃 / 興櫃 / ETF / 已下市）
 3. 官方每日增量：證交所 + 櫃買中心全市場資料（最近 30 天缺的交易日）
 4. FinMind：補缺口、定期更新月營收 / 財報 / 除權息，然後逐檔回補歷史
 5. 集保：每週大戶持股快照 + 過去一年回補
 6. 匯出：輪流更新個股 Excel
 7. 統計：更新 Google 試算表「資料庫統計」與網頁儀表板

用法：
  python -m twstock_db.agent                 # 正常執行
  python -m twstock_db.agent --only daily    # 只跑某一步（daily/finmind/holding/export/stats）
  python -m twstock_db.agent --rebuild-stats # 重新統計覆蓋率
"""
from __future__ import annotations

import argparse
import os
import shutil
import time
import traceback
from pathlib import Path

import pandas as pd

from . import daily, export_excel, holding, planner, stats, universe
from .config import REPO_ROOT, load_settings
from .sources.finmind import FinMind
from .storage.backend import make_backend
from .storage.store import Store
from .util import Deadline, iso_now, log, setup_logging


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="台股歷史資料庫爬蟲 agent")
    ap.add_argument("--cache", default=os.environ.get("DB_CACHE_DIR", str(REPO_ROOT / ".dbcache")))
    ap.add_argument("--site", default=str(REPO_ROOT / "site_out"), help="儀表板輸出資料夾")
    ap.add_argument("--only", choices=["daily", "finmind", "holding", "export", "stats"])
    ap.add_argument("--minutes", type=float, default=None, help="總時間預算（分鐘）")
    ap.add_argument("--rebuild-stats", action="store_true")
    args = ap.parse_args(argv)

    setup_logging()
    s = load_settings()
    total = args.minutes or s.budget("total", 50)
    deadline = Deadline(total)
    t0 = time.time()
    run = {"time": iso_now(), "status": "ok", "official_requests": 0, "official_rows": 0,
           "official_dates": "", "finmind_requests": 0, "finmind_rows": 0, "finmind_quota_hit": False,
           "backfill_pending": "", "tdcc_queries": 0, "tdcc_rows": 0, "excel_exported": 0,
           "excel_pending": "", "uploaded": 0, "errors": ""}
    errors: list[str] = []

    backend = make_backend(s.drive_folder)
    store = Store(args.cache, backend)
    log.info("雲端資料庫：%s（%d 個檔案）", backend.folder_url(), len(store.remote))
    fm = FinMind()

    def step(name):
        return args.only is None or args.only == name

    # ---- 股票清單
    info = store.read_meta("stock_info")
    if universe.needs_refresh(info):
        try:
            info = universe.refresh_from_finmind(fm, info)
            store.write_meta("stock_info", info)
            log.info("股票清單更新：%d 檔", len(info))
        except Exception as e:  # noqa: BLE001
            log.warning("股票清單更新失敗（沿用舊清單）：%s", e)
            errors.append(f"stock_info: {e}")
    if info.empty:
        info = pd.DataFrame(columns=universe.COLS)

    # ---- 官方每日
    if step("daily"):
        try:
            d = daily.run_daily(store, s, Deadline(min(s.budget("official_daily", 12), deadline.left() / 60)))
            run.update(official_requests=d["requests"], official_rows=d["rows"],
                       official_dates=",".join(d["dates"][-5:]))
            errors += d["errors"][:5]
            store.flush()
        except Exception as e:  # noqa: BLE001
            log.error("官方每日更新失敗：%s", traceback.format_exc())
            errors.append(f"daily: {e}")

    uni = universe.filter_universe(info, s)
    latest = daily.latest_trading_date(store)

    # ---- FinMind
    if step("finmind") and not uni.empty:
        try:
            f = planner.run_finmind(store, uni, s, Deadline(min(s.budget("finmind", 22), deadline.left() / 60)),
                                    latest, fm)
            run.update(finmind_requests=f["requests"], finmind_rows=f["rows"],
                       finmind_quota_hit=f["quota_hit"], backfill_pending=str(f["pending_backfill"]))
            errors += f["errors"][:5]
            store.flush()
            store.sync()  # 先把抓到的資料存上雲端，避免後面步驟出錯而遺失
        except Exception as e:  # noqa: BLE001
            log.error("FinMind 步驟失敗：%s", traceback.format_exc())
            errors.append(f"finmind: {e}")

    # ---- 集保
    if step("holding") and not uni.empty:
        try:
            h = holding.run_holding(store, uni, s, deadline, fm)
            run.update(tdcc_queries=h["backfill_queries"], tdcc_rows=h["rows"])
            errors += h["errors"][:5]
            store.flush()
        except Exception as e:  # noqa: BLE001
            log.error("集保步驟失敗：%s", traceback.format_exc())
            errors.append(f"holding: {e}")

    if args.rebuild_stats:
        store.flush()
        store.rebuild_coverage()

    # ---- 匯出個股 Excel
    if step("export") and not uni.empty:
        try:
            ex = export_excel.run_export(store, uni, int(s.excel_per_run),
                                         Deadline(min(s.budget("export", 8), max(0.5, deadline.left() / 60 - 3))))
            run.update(excel_exported=ex["exported"], excel_pending=str(ex["pending"]))
        except Exception as e:  # noqa: BLE001
            log.error("匯出失敗：%s", traceback.format_exc())
            errors.append(f"export: {e}")

    # ---- 執行紀錄 + 統計
    run["errors"] = " | ".join(errors)[:1000]
    run["status"] = "ok" if not errors else "部分錯誤"
    run["minutes"] = round((time.time() - t0) / 60, 1)
    try:
        run["uploaded"] = store.sync()
    except Exception as e:  # noqa: BLE001
        log.error("上傳失敗：%s", traceback.format_exc())
        run["status"] = "上傳失敗"
        run["errors"] = (run["errors"] + f" | sync: {e}")[:1000]
    runs = store.read_meta("run_log")
    runs = pd.concat([runs, pd.DataFrame([run])], ignore_index=True).tail(500)
    for c in runs.columns:
        if runs[c].dtype == object:
            runs[c] = runs[c].astype(str)
    store.write_meta("run_log", runs)

    try:
        st = stats.compute(store, uni, s)
        stats.write_sheet(store, st)
        site = Path(args.site)
        stats.write_json(st, site)
        shutil.copyfile(REPO_ROOT / "dashboard" / "index.html", site / "index.html")
    except Exception as e:  # noqa: BLE001
        log.error("統計失敗（%s）：%s", e, traceback.format_exc())
    store.sync()
    log.info("完成：%s", {k: v for k, v in run.items() if v not in ("", 0, False)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
