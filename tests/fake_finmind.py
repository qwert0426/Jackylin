"""測試用的假 FinMind：回傳和 FinMind 文件相同欄位的合成資料，不連網、不耗額度。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from twstock_db.sources.finmind import QuotaExceeded

INFO = pd.DataFrame([
    {"industry_category": "半導體業", "stock_id": "2330", "stock_name": "台積電", "type": "twse", "date": "2026-11-20"},
    {"industry_category": "半導體業", "stock_id": "6488", "stock_name": "環球晶", "type": "tpex", "date": "2026-11-20"},
    {"industry_category": "ETF", "stock_id": "0050", "stock_name": "元大台灣50", "type": "twse", "date": "2026-11-20"},
    {"industry_category": "食品工業", "stock_id": "1260", "stock_name": "富味鄉", "type": "emerging", "date": "2026-11-20"},
    {"industry_category": "Index", "stock_id": "TAIEX", "stock_name": "加權指數", "type": "twse", "date": "2026-11-20"},
    {"industry_category": "權證", "stock_id": "030001", "stock_name": "權證", "type": "twse", "date": "2026-11-20"},
])
DELIST = pd.DataFrame([
    {"date": "2019-06-03", "stock_id": "2311", "stock_name": "日月光"},
    {"date": "2005-01-01", "stock_id": "1111", "stock_name": "很早下市"},
])


def _days(start, end="2026-11-20"):
    return pd.bdate_range(max(pd.Timestamp(start), pd.Timestamp("2010-01-04")), end)


class FakeFinMind:
    def __init__(self, quota_after: int | None = None):
        self.requests_made = 0
        self.quota_after = quota_after
        self.calls = []

    def remaining_quota(self):
        return None

    def get(self, dataset, data_id=None, start_date="2010-01-01", end_date=None):
        if self.quota_after is not None and self.requests_made >= self.quota_after:
            raise QuotaExceeded("fake 402")
        self.requests_made += 1
        self.calls.append((dataset, data_id, start_date))
        if dataset == "TaiwanStockInfo":
            return INFO.copy()
        if dataset == "TaiwanStockDelisting":
            return DELIST.copy()
        end = "2019-05-31" if data_id == "2311" else "2026-11-20"
        if data_id == "1260" and dataset not in ("TaiwanStockPrice", "TaiwanStockMonthRevenue",
                                                 "TaiwanStockFinancialStatements",
                                                 "TaiwanStockBalanceSheet", "TaiwanStockCashFlowsStatement"):
            return pd.DataFrame()
        rng = np.random.default_rng(abs(hash((dataset, data_id))) % 2**32)
        if dataset == "TaiwanStockPrice":
            d = _days(start_date, end)
            close = 100 + rng.normal(0, 1, len(d)).cumsum()
            return pd.DataFrame({"date": d.strftime("%Y-%m-%d"), "stock_id": data_id,
                                 "Trading_Volume": 1000000, "Trading_money": 1e8, "open": close,
                                 "max": close + 1, "min": close - 1, "close": close, "spread": 0.5,
                                 "Trading_turnover": 500})
        if dataset == "TaiwanStockPER":
            d = _days(start_date, end)
            return pd.DataFrame({"date": d.strftime("%Y-%m-%d"), "stock_id": data_id,
                                 "dividend_yield": 2.0, "PER": 15.0, "PBR": 2.0})
        if dataset == "TaiwanStockInstitutionalInvestorsBuySell":
            d = _days(start_date, end)
            rows = [{"date": x, "stock_id": data_id, "buy": 1000, "name": n, "sell": 500}
                    for x in d.strftime("%Y-%m-%d") for n in
                    ["Foreign_Investor", "Investment_Trust", "Dealer_self", "Dealer_Hedging",
                     "Foreign_Dealer_Self"]]
            return pd.DataFrame(rows)
        if dataset == "TaiwanStockMonthRevenue":
            m = pd.date_range(max(pd.Timestamp(start_date), pd.Timestamp("2010-02-01")), end, freq="MS")
            return pd.DataFrame({"date": m.strftime("%Y-%m-%d"), "stock_id": data_id, "country": "Taiwan",
                                 "revenue": rng.integers(1e9, 2e9, len(m)),
                                 "revenue_month": (m - pd.DateOffset(months=1)).month,
                                 "revenue_year": (m - pd.DateOffset(months=1)).year})
        if dataset in ("TaiwanStockFinancialStatements", "TaiwanStockBalanceSheet",
                       "TaiwanStockCashFlowsStatement"):
            q = pd.date_range(max(pd.Timestamp(start_date), pd.Timestamp("2010-03-31")), "2026-09-30", freq="QE")
            types = {"TaiwanStockFinancialStatements": ["Revenue", "GrossProfit", "OperatingIncome",
                                                        "EPS", "IncomeAfterTaxes",
                                                        "EquityAttributableToOwnersOfParent"],
                     "TaiwanStockBalanceSheet": ["TotalAssets", "Equity",
                                                 "EquityAttributableToOwnersOfParent"],
                     "TaiwanStockCashFlowsStatement": ["CashFlowsFromOperatingActivities"]}[dataset]
            val = {"Revenue": 1000, "GrossProfit": 400, "OperatingIncome": 300, "EPS": 3.2,
                   "IncomeAfterTaxes": 250, "EquityAttributableToOwnersOfParent": 240,
                   "TotalAssets": 9000, "Equity": 5000, "CashFlowsFromOperatingActivities": 500}
            rows = [{"date": x, "stock_id": data_id, "type": t, "value": val[t], "origin_name": t}
                    for x in q.strftime("%Y-%m-%d") for t in types]
            return pd.DataFrame(rows)
        if dataset == "TaiwanStockDividendResult":
            return pd.DataFrame([{"date": "2026-06-12", "stock_id": data_id, "before_price": 110.0,
                                  "after_price": 100.0, "stock_and_cache_dividend": 10.0,
                                  "stock_or_cache_dividend": "息", "max_price": 110, "min_price": 90,
                                  "open_price": 100, "reference_price": 100}])
        if dataset == "TaiwanStockCapitalReductionReferencePrice":
            return pd.DataFrame()
        d = _days(start_date, end)
        if dataset == "TaiwanStockMarginPurchaseShortSale":
            return pd.DataFrame({"date": d.strftime("%Y-%m-%d"), "stock_id": data_id,
                                 "MarginPurchaseBuy": 10, "MarginPurchaseTodayBalance": 100,
                                 "ShortSaleTodayBalance": 5, "Note": ""})
        if dataset == "TaiwanStockShareholding":
            return pd.DataFrame({"date": d.strftime("%Y-%m-%d"), "stock_id": data_id,
                                 "stock_name": "x", "ForeignInvestmentSharesRatio": 70.0})
        if dataset == "TaiwanDailyShortSaleBalances":
            return pd.DataFrame({"date": d.strftime("%Y-%m-%d"), "stock_id": data_id,
                                 "SBLShortSalesCurrentDayBalance": 1000.0})
        if dataset == "TaiwanStockDayTrading":
            return pd.DataFrame({"date": d.strftime("%Y-%m-%d"), "stock_id": data_id,
                                 "BuyAfterSale": "", "Volume": 100, "BuyAmount": 1e6, "SellAmount": 1e6})
        raise ValueError(dataset)
