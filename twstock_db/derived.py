"""衍生資料：還原股價、EPS / 毛利率 / ROE、法人買賣超寬表、大戶持股比例。

這些都從原始資料即時計算（不另外存檔），選股 / 回測程式也可以直接 import 使用。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def adjust_factors(dividend: pd.DataFrame, capital_reduction: pd.DataFrame | None = None) -> pd.DataFrame:
    """每個除權息 / 減資事件的調整比例 ratio = 參考價 / 前一日收盤價。

    回傳 stock_id, date, ratio。
    """
    parts = []
    if dividend is not None and not dividend.empty and {"before_price", "after_price"} <= set(dividend):
        d = dividend[["stock_id", "date", "before_price", "after_price"]].copy()
        d["ratio"] = d["after_price"] / d["before_price"]
        parts.append(d[["stock_id", "date", "ratio"]])
    if capital_reduction is not None and not capital_reduction.empty:
        cr = capital_reduction
        before = next((c for c in cr.columns if "ClosingPrice" in c), None)
        after = next((c for c in cr.columns if "PostReductionReferencePrice" in c), None)
        if before and after:
            c = cr[["stock_id", "date", before, after]].copy()
            c["ratio"] = c[after] / c[before]
            parts.append(c[["stock_id", "date", "ratio"]])
    if not parts:
        return pd.DataFrame(columns=["stock_id", "date", "ratio"])
    ev = pd.concat(parts, ignore_index=True)
    ev = ev[np.isfinite(ev["ratio"]) & (ev["ratio"] > 0) & (ev["ratio"] < 10)]
    return ev.groupby(["stock_id", "date"], as_index=False)["ratio"].prod()


def adjusted_prices(price: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """還原權值股價（向前調整：最新價格 = 實際價格，過去價格依除權息比例往下調）。

    新增欄位 adj_factor, adj_open, adj_max, adj_min, adj_close。
    """
    if price.empty:
        return price
    out = []
    ev_by = {k: g for k, g in events.groupby("stock_id")} if not events.empty else {}
    for sid, g in price.sort_values(["stock_id", "date"]).groupby("stock_id", sort=False):
        g = g.copy()
        ev = ev_by.get(sid)
        factor = np.ones(len(g))
        if ev is not None and not ev.empty:
            dates = g["date"].to_numpy()
            for d, r in zip(ev["date"], ev["ratio"]):
                factor[dates < d] *= r
        g["adj_factor"] = factor
        for c in ["open", "max", "min", "close"]:
            if c in g:
                g[f"adj_{c}"] = (g[c] * factor).round(4)
        out.append(g)
    return pd.concat(out, ignore_index=True)


def institutional_wide(inst: pd.DataFrame) -> pd.DataFrame:
    """三大法人長表 → 寬表（外資 / 投信 / 自營商 買賣超股數）。"""
    if inst.empty:
        return pd.DataFrame()
    x = inst.copy()
    x["net"] = x["buy"].fillna(0) - x["sell"].fillna(0)
    grp = {"Foreign_Investor": "外資", "Foreign_Dealer_Self": "外資", "Investment_Trust": "投信",
           "Dealer_self": "自營商", "Dealer_Hedging": "自營商", "Dealer": "自營商"}
    x["who"] = x["name"].map(grp)
    x = x[x["who"].notna()]
    w = x.pivot_table(index=["stock_id", "date"], columns="who", values="net", aggfunc="sum").reset_index()
    for c in ["外資", "投信", "自營商"]:
        if c not in w:
            w[c] = 0.0
    w["三大法人合計"] = w[["外資", "投信", "自營商"]].sum(axis=1)
    return w


def quarterly_ratios(income: pd.DataFrame, balance: pd.DataFrame | None = None) -> pd.DataFrame:
    """季 EPS、毛利率、營業利益率、稅後淨利，以及年 ROE（只在第四季列出）。"""
    if income is None or income.empty:
        return pd.DataFrame()
    inc = income.pivot_table(index=["stock_id", "date"], columns="type", values="value",
                             aggfunc="last").reset_index()
    out = pd.DataFrame({"stock_id": inc["stock_id"], "date": inc["date"]})

    def col(df, *names):
        for n in names:
            if n in df:
                return df[n]
        return pd.Series(np.nan, index=df.index)

    rev = col(inc, "Revenue")
    out["EPS"] = col(inc, "EPS")
    out["營業收入"] = rev
    out["毛利率(%)"] = (col(inc, "GrossProfit") / rev * 100).round(2)
    out["營業利益率(%)"] = (col(inc, "OperatingIncome") / rev * 100).round(2)
    ni = col(inc, "EquityAttributableToOwnersOfParent", "IncomeAfterTaxes", "NetIncome")
    out["稅後淨利"] = ni
    out["ROE(年,%)"] = np.nan
    if balance is not None and not balance.empty:
        bal = balance[balance["type"].isin(["EquityAttributableToOwnersOfParent", "Equity"])]
        bal = bal.sort_values("type").drop_duplicates(["stock_id", "date"], keep="last")
        eq = bal.set_index(["stock_id", "date"])["value"]
        out["_y"] = out["date"].str[:4]
        annual_ni = out.groupby(["stock_id", "_y"])["稅後淨利"].transform(
            lambda s: s.sum() if s.notna().sum() == 4 else np.nan)
        is_q4 = out["date"].str[5:] == "12-31"
        roes = []
        for sid, d, ani, q4 in zip(out["stock_id"], out["date"], annual_ni, is_q4):
            if not q4 or ani != ani:
                roes.append(np.nan)
                continue
            prev = f"{int(d[:4]) - 1}-12-31"
            e1, e0 = eq.get((sid, d), np.nan), eq.get((sid, prev), np.nan)
            avg = np.nanmean([e1, e0]) if (e1 == e1 or e0 == e0) else np.nan
            roes.append(round(ani / avg * 100, 2) if avg and avg == avg else np.nan)
        out["ROE(年,%)"] = roes
        out = out.drop(columns="_y")
    return out


def revenue_growth(rev: pd.DataFrame) -> pd.DataFrame:
    """月營收加上月增率 / 年增率。"""
    if rev.empty:
        return rev
    r = rev.sort_values(["stock_id", "date"]).copy()
    r["月增率(%)"] = (r.groupby("stock_id")["revenue"].pct_change(fill_method=None) * 100).round(2)
    r["_ym"] = r["revenue_year"].astype("Int64").astype(str) + "-" + r["revenue_month"].astype("Int64").astype(str)
    prev = r.assign(_ym=(r["revenue_year"] + 1).astype("Int64").astype(str) + "-" +
                    r["revenue_month"].astype("Int64").astype(str))[["stock_id", "_ym", "revenue"]]
    r = r.merge(prev.rename(columns={"revenue": "_ly"}), on=["stock_id", "_ym"], how="left")
    r["年增率(%)"] = ((r["revenue"] / r["_ly"] - 1) * 100).round(2)
    return r.drop(columns=["_ym", "_ly"])
