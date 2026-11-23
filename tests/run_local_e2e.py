"""端對端測試：真的連證交所 / 櫃買 / 集保，FinMind 用假資料，雲端硬碟用本機資料夾。

用法：python tests/run_local_e2e.py <工作資料夾> [執行次數]
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

work = Path(sys.argv[1])
runs = int(sys.argv[2]) if len(sys.argv) > 2 else 1
quota = int(os.environ.get("FAKE_QUOTA", "200"))
work.mkdir(parents=True, exist_ok=True)
cfg = work / "config.yaml"
cfg.write_text(
    "start_date: '2010-01-01'\n"
    "finmind_max_requests_per_run: 40\n"
    "time_budget: {total: 15, official_daily: 6, finmind: 3, tdcc_backfill: 0.4, export: 2}\n"
    "excel_per_run: 3\n", encoding="utf-8")
os.environ["CRAWLER_CONFIG"] = str(cfg)
os.environ["LOCAL_DRIVE_DIR"] = str(work / "drive")
os.environ.pop("GOOGLE_REFRESH_TOKEN", None)

from fake_finmind import FakeFinMind  # noqa: E402

from twstock_db import agent, daily  # noqa: E402

daily.CATCHUP_DAYS = int(os.environ.get("CATCHUP_DAYS", "4"))
fake = FakeFinMind(quota_after=quota)
agent.FinMind = lambda: fake
for i in range(runs):
    fake.requests_made = 0
    agent.main(["--cache", str(work / "cache"), "--site", str(work / "site")])
print("FAKE FINMIND CALLS:", len(fake.calls))
