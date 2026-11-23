"""FinMind API（https://finmindtrade.com/）— 逐檔回補歷史資料。

免費會員（register）每小時 600 次請求。每次請求可以拿到一檔股票某資料集的完整歷史。
"""
from __future__ import annotations

import os
import time

import pandas as pd
import requests

from ..util import log

API = "https://api.finmindtrade.com/api/v4/data"
USER_INFO = "https://api.web.finmindtrade.com/v2/user_info"


class QuotaExceeded(Exception):
    """FinMind 額度用完（HTTP 402）。"""


class LevelRequired(Exception):
    """該資料集需要付費會員。"""


class FinMind:
    def __init__(self, token: str | None = None, min_interval: float = 0.4):
        self.token = token if token is not None else os.environ.get("FINMIND_TOKEN", "")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "twstock-db-crawler/1.0"
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
        self.min_interval = min_interval
        self._last = 0.0
        self.requests_made = 0

    def remaining_quota(self) -> int | None:
        """查詢本小時剩餘額度；查不到回傳 None。"""
        if not self.token:
            return None
        try:
            r = self.session.get(USER_INFO, params={"token": self.token}, timeout=20)
            d = r.json()
            used, limit = int(d.get("user_count", 0)), int(d.get("api_request_limit", 600))
            return max(0, limit - used)
        except Exception as e:  # noqa: BLE001
            log.warning("FinMind 額度查詢失敗：%s", e)
            return None

    def get(self, dataset: str, data_id: str | None = None, start_date: str = "2010-01-01",
            end_date: str | None = None, retries: int = 3) -> pd.DataFrame:
        params = {"dataset": dataset, "start_date": start_date}
        if data_id:
            params["data_id"] = data_id
        if end_date:
            params["end_date"] = end_date
        for attempt in range(retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(API, params=params, timeout=90)
                self.requests_made += 1
            except requests.RequestException as e:
                log.warning("FinMind 連線錯誤 %s %s：%s", dataset, data_id, e)
                time.sleep(5 * (attempt + 1))
                continue
            try:
                d = r.json()
            except ValueError:
                d = {"status": r.status_code, "msg": r.text[:200]}
            status = d.get("status", r.status_code)
            if status == 402 or r.status_code == 402:
                raise QuotaExceeded(d.get("msg", "402"))
            if status == 400 and "level" in str(d.get("msg", "")).lower():
                raise LevelRequired(d.get("msg"))
            if status == 200:
                return pd.DataFrame(d.get("data") or [])
            log.warning("FinMind 回應 %s %s %s：%s", status, dataset, data_id, d.get("msg"))
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"FinMind {dataset} {data_id} 重試 {retries} 次仍失敗")
