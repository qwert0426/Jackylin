"""本機 Parquet 資料庫 + 與雲端同步。

雲端（Google Drive）上的結構：
  data/<資料集>/<年>.parquet     所有股票、該年資料（給程式 / 回測用）
  meta/*.parquet                 進度、覆蓋率、執行紀錄
  個股Excel/<市場>/<代號>_<名稱>.xlsx   給手機 / 電腦直接看

本機有一份快取（GitHub Actions 用 actions/cache 保留），每次執行先比對雲端 md5，
只下載有變動的檔案；寫入後只上傳有變動的檔案。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..config import DATASETS, TEXT_COLS
from ..util import log, normalize_frame
from .backend import md5_file

COVERAGE_COLS = ["dataset", "year", "stock_id", "rows", "min_date", "max_date"]


class Store:
    def __init__(self, cache_dir: str | Path, backend):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.backend = backend
        self.remote = backend.list_all()
        self._buffers: dict[str, list[pd.DataFrame]] = {}
        self._buffer_rows = 0
        self.dirty: set[str] = set()
        self.rows_written: dict[str, int] = {}
        self._coverage: pd.DataFrame | None = None
        self._manifest_path = self.cache / ".manifest.json"
        try:
            self._manifest = json.loads(self._manifest_path.read_text())
        except Exception:  # noqa: BLE001
            self._manifest = {}

    # ---------------------------------------------------------------- files
    def local(self, rel: str) -> Path:
        return self.cache / rel

    def ensure_local(self, rel: str) -> Path | None:
        """確保本機有雲端最新版本；雲端沒有這個檔案就回傳本機檔（可能不存在）。"""
        p = self.local(rel)
        info = self.remote.get(rel)
        if info is None:
            return p if p.exists() else None
        if p.exists() and info.get("md5") and self._manifest.get(rel) == info["md5"]:
            return p
        if p.exists() and info.get("md5") and md5_file(p) == info["md5"]:
            self._manifest[rel] = info["md5"]
            return p
        log.info("下載 %s", rel)
        self.backend.download(rel, p)
        self._manifest[rel] = info.get("md5") or md5_file(p)
        return p

    def remote_files(self, prefix: str) -> list[str]:
        return sorted(r for r in self.remote if r.startswith(prefix))

    def read_parquet(self, rel: str, **kw) -> pd.DataFrame:
        p = self.ensure_local(rel)
        if p is None or not p.exists():
            return pd.DataFrame()
        return pq.read_table(p, **kw).to_pandas()

    def write_parquet(self, rel: str, df: pd.DataFrame) -> None:
        p = self.local(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pandas(df.reset_index(drop=True), preserve_index=False)
        pq.write_table(table, p, compression="zstd")
        self.dirty.add(rel)

    def mark_dirty(self, rel: str) -> None:
        self.dirty.add(rel)

    # ---------------------------------------------------------------- datasets
    def year_files(self, ds: str) -> list[str]:
        names = set(self.remote_files(f"data/{ds}/"))
        local_dir = self.local(f"data/{ds}")
        if local_dir.exists():
            names |= {f"data/{ds}/{p.name}" for p in local_dir.glob("*.parquet")}
        return sorted(n for n in names if n.endswith(".parquet"))

    def upsert(self, ds: str, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        self._buffers.setdefault(ds, []).append(df)
        self._buffer_rows += len(df)
        if self._buffer_rows > 3_000_000:
            self.flush()

    def flush(self) -> None:
        for ds, parts in list(self._buffers.items()):
            if parts:
                self._merge(ds, pd.concat(parts, ignore_index=True))
        self._buffers.clear()
        self._buffer_rows = 0

    def _merge(self, ds: str, new: pd.DataFrame) -> None:
        keys = list(DATASETS[ds].keys)
        new = normalize_frame(new, TEXT_COLS)
        new = new.dropna(subset=[k for k in keys if k in new.columns])
        new["_year"] = new["date"].str[:4]
        for year, part in new.groupby("_year"):
            rel = f"data/{ds}/{year}.parquet"
            old = self.read_parquet(rel)
            part = part.drop(columns="_year")
            merged = pd.concat([old, part], ignore_index=True) if not old.empty else part
            merged = normalize_frame(merged, TEXT_COLS)
            before = len(old)
            merged = merged.drop_duplicates(subset=keys, keep="last")
            merged = merged.sort_values(["stock_id"] + [k for k in keys if k != "stock_id"])
            self.write_parquet(rel, merged)
            self.rows_written[ds] = self.rows_written.get(ds, 0) + max(0, len(merged) - before)
            self._update_coverage(ds, int(year), merged)

    # ---------------------------------------------------------------- coverage
    def coverage(self) -> pd.DataFrame:
        if self._coverage is None:
            cov = self.read_parquet("meta/coverage.parquet")
            self._coverage = cov if not cov.empty else pd.DataFrame(columns=COVERAGE_COLS)
        return self._coverage

    def _update_coverage(self, ds: str, year: int, merged: pd.DataFrame) -> None:
        g = merged.groupby("stock_id")["date"].agg(["size", "min", "max"]).reset_index()
        g.columns = ["stock_id", "rows", "min_date", "max_date"]
        g.insert(0, "year", year)
        g.insert(0, "dataset", ds)
        cov = self.coverage()
        cov = cov[~((cov["dataset"] == ds) & (cov["year"] == year))]
        self._coverage = pd.concat([cov, g], ignore_index=True) if not cov.empty else g
        self._coverage["year"] = self._coverage["year"].astype(int)
        self._coverage["rows"] = self._coverage["rows"].astype(int)
        self.write_parquet("meta/coverage.parquet", self._coverage)

    def rebuild_coverage(self) -> None:
        """從所有資料檔重新統計覆蓋率（統計介面的「重新統計」）。"""
        self._coverage = pd.DataFrame(columns=COVERAGE_COLS)
        for ds in DATASETS:
            for rel in self.year_files(ds):
                df = self.read_parquet(rel, columns=["stock_id", "date"])
                if not df.empty:
                    self._update_coverage(ds, int(Path(rel).stem), df)

    def stock_summary(self, ds: str) -> pd.DataFrame:
        """每檔股票在某資料集的 rows / min_date / max_date（跨年彙總）。"""
        cov = self.coverage()
        cov = cov[cov["dataset"] == ds]
        if cov.empty:
            return pd.DataFrame(columns=["stock_id", "rows", "min_date", "max_date"])
        return cov.groupby("stock_id").agg(rows=("rows", "sum"), min_date=("min_date", "min"),
                                           max_date=("max_date", "max")).reset_index()

    def load(self, ds: str, stock_ids=None, columns=None) -> pd.DataFrame:
        """讀整個資料集（可篩股票）。"""
        filters = [("stock_id", "in", list(stock_ids))] if stock_ids is not None else None
        parts = []
        for rel in self.year_files(ds):
            p = self.ensure_local(rel)
            if p and p.exists():
                parts.append(pq.read_table(p, filters=filters, columns=columns).to_pandas())
        parts = [p for p in parts if not p.empty]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    # ---------------------------------------------------------------- meta tables
    def read_meta(self, name: str) -> pd.DataFrame:
        return self.read_parquet(f"meta/{name}.parquet")

    def write_meta(self, name: str, df: pd.DataFrame) -> None:
        self.write_parquet(f"meta/{name}.parquet", df)

    # ---------------------------------------------------------------- sync
    def sync(self) -> int:
        """把有變動的檔案上傳到雲端。回傳上傳數量。"""
        self.flush()
        n = 0
        for rel in sorted(self.dirty):
            p = self.local(rel)
            if not p.exists():
                continue
            md5 = md5_file(p)
            if self.remote.get(rel, {}).get("md5") == md5:
                continue
            mime = None
            if rel.endswith(".xlsx"):
                mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            self.backend.upload(p, rel, mime)
            # 只更新 md5/size，保留後端記錄的檔案 id（Google Drive 與 Store 共用同一份索引）
            self.remote.setdefault(rel, {}).update(md5=md5, size=p.stat().st_size)
            self._manifest[rel] = md5
            n += 1
        self.dirty.clear()
        self._manifest_path.write_text(json.dumps(self._manifest))
        if n:
            log.info("已上傳 %d 個檔案到雲端", n)
        return n
