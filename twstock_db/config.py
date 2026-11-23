"""設定檔與資料集登錄表。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# 股票類型
KIND_STOCK = "stock"
KIND_ETF = "etf"
# 市場
MKT_LISTED = "上市"
MKT_OTC = "上櫃"
MKT_EMERGING = "興櫃"

FINMIND = "FinMind (api.finmindtrade.com)"
TWSE = "臺灣證券交易所 (www.twse.com.tw)"
TPEX = "證券櫃檯買賣中心 (www.tpex.org.tw)"
TDCC = "臺灣集中保管結算所 (opendata.tdcc.com.tw / www.tdcc.com.tw)"


@dataclass(frozen=True)
class Dataset:
    name: str                 # 程式內部代號，也是 Drive 資料夾名稱
    label: str                # 中文名稱
    category: str             # 基本面 / 籌碼面 / 技術面
    keys: tuple               # 去重複用的主鍵
    finmind: str | None       # FinMind dataset 名稱
    kinds: frozenset          # 適用的股票類型
    markets: frozenset        # 適用的市場
    freq: str                 # daily / monthly / quarterly / event / weekly
    official: bool = False    # 每日增量由官方全市場 API 提供
    sources: tuple = field(default_factory=tuple)


ALL_MKTS = frozenset({MKT_LISTED, MKT_OTC, MKT_EMERGING})
LISTED_OTC = frozenset({MKT_LISTED, MKT_OTC})
STOCK = frozenset({KIND_STOCK})
STOCK_ETF = frozenset({KIND_STOCK, KIND_ETF})

DATASETS: dict[str, Dataset] = {d.name: d for d in [
    Dataset("price", "日K價量", "技術面", ("date", "stock_id"), "TaiwanStockPrice",
            STOCK_ETF, ALL_MKTS, "daily", True, (TWSE, TPEX, FINMIND)),
    Dataset("per", "本益比 / 淨值比 / 殖利率", "基本面", ("date", "stock_id"), "TaiwanStockPER",
            STOCK, LISTED_OTC, "daily", True, (TWSE, TPEX, FINMIND)),
    Dataset("institutional", "三大法人買賣超", "籌碼面", ("date", "stock_id", "name"),
            "TaiwanStockInstitutionalInvestorsBuySell", STOCK_ETF, LISTED_OTC, "daily", True,
            (TWSE, TPEX, FINMIND)),
    Dataset("margin", "融資融券餘額", "籌碼面", ("date", "stock_id"),
            "TaiwanStockMarginPurchaseShortSale", STOCK_ETF, LISTED_OTC, "daily", True,
            (TWSE, TPEX, FINMIND)),
    Dataset("foreign_holding", "外資持股比例", "籌碼面", ("date", "stock_id"),
            "TaiwanStockShareholding", STOCK_ETF, LISTED_OTC, "daily", True, (TWSE, TPEX, FINMIND)),
    Dataset("short_balance", "借券賣出餘額", "籌碼面", ("date", "stock_id"),
            "TaiwanDailyShortSaleBalances", STOCK_ETF, LISTED_OTC, "daily", True,
            (TWSE, TPEX, FINMIND)),
    Dataset("day_trading", "當沖成交量 / 金額", "技術面", ("date", "stock_id"),
            "TaiwanStockDayTrading", STOCK_ETF, LISTED_OTC, "daily", True, (TWSE, TPEX, FINMIND)),
    Dataset("month_revenue", "月營收", "基本面", ("date", "stock_id"), "TaiwanStockMonthRevenue",
            STOCK, ALL_MKTS, "monthly", False, (FINMIND,)),
    Dataset("income", "綜合損益表（EPS / 毛利）", "基本面", ("date", "stock_id", "type"),
            "TaiwanStockFinancialStatements", STOCK, ALL_MKTS, "quarterly", False, (FINMIND,)),
    Dataset("balance", "資產負債表", "基本面", ("date", "stock_id", "type"),
            "TaiwanStockBalanceSheet", STOCK, ALL_MKTS, "quarterly", False, (FINMIND,)),
    Dataset("cashflow", "現金流量表", "基本面", ("date", "stock_id", "type"),
            "TaiwanStockCashFlowsStatement", STOCK, ALL_MKTS, "quarterly", False, (FINMIND,)),
    Dataset("dividend", "除權息結果", "基本面", ("date", "stock_id"), "TaiwanStockDividendResult",
            STOCK_ETF, LISTED_OTC, "event", False, (FINMIND,)),
    Dataset("capital_reduction", "減資參考價", "基本面", ("date", "stock_id"),
            "TaiwanStockCapitalReductionReferencePrice", STOCK, LISTED_OTC, "event", False,
            (FINMIND,)),
    Dataset("holding", "集保股權分散（大戶持股）", "籌碼面", ("date", "stock_id", "level"), None,
            STOCK_ETF, ALL_MKTS, "weekly", False, (TDCC,)),
]}

# 非數值欄位（其他欄位一律轉成數字）
TEXT_COLS = {"date", "stock_id", "stock_name", "name", "type", "origin_name", "Note", "note",
             "InternationalCode", "RecentlyDeclareDate", "country", "BuyAfterSale",
             "stock_or_cache_dividend", "ReasonforCapitalReduction"}


@dataclass
class Settings:
    start_date: str = "2010-01-01"
    drive_folder: str = "台股資料庫"
    datasets: dict = field(default_factory=dict)
    universe: dict = field(default_factory=dict)
    finmind_max_requests_per_run: int = 550
    finmind_sponsor: bool = False
    time_budget: dict = field(default_factory=dict)
    excel_per_run: int = 150

    def enabled(self, ds: str) -> bool:
        return bool(self.datasets.get(ds, True))

    def budget(self, key: str, default: float) -> float:
        return float(self.time_budget.get(key, default))


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("CRAWLER_CONFIG", REPO_ROOT / "crawler_config.yaml"))
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    s = Settings()
    for k, v in raw.items():
        if hasattr(s, k) and v is not None:
            setattr(s, k, v)
    s.start_date = str(s.start_date)
    return s
