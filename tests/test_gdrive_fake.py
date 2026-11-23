"""用假的 Google Drive / Sheets API 測試 GDriveBackend 的流程（不連網）。"""
import hashlib
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import googleapiclient.discovery  # noqa: E402
import googleapiclient.http  # noqa: E402

from twstock_db.storage import backend as be  # noqa: E402
from twstock_db.storage.store import Store  # noqa: E402


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeDrive:
    def __init__(self):
        self.files_db = {}
        self.n = 0
        self.calls = []

    def files(self):
        return self

    def _new_id(self):
        self.n += 1
        return f"id{self.n}"

    def list(self, q, fields=None, pageSize=None, pageToken=None):
        def run():
            self.calls.append(("list", q))
            items = [f for f in self.files_db.values() if not f["trashed"]]
            m = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
            if m:
                items = [f for f in items if f["name"] == m.group(1).replace("\\'", "'")]
            m = re.search(r"mimeType = '([^']+)'", q)
            if m:
                items = [f for f in items if f["mimeType"] == m.group(1)]
            m = re.search(r"'([^']+)' in parents", q)
            if m:
                items = [f for f in items if m.group(1) in f["parents"]]
            out = [{"id": f["id"], "name": f["name"], "mimeType": f["mimeType"],
                    "md5Checksum": hashlib.md5(f["content"]).hexdigest() if f["content"] is not None else None,
                    "size": str(len(f["content"] or b""))} for f in items]
            return {"files": out}
        return _Req(run)

    def create(self, body, media_body=None, fields=None):
        def run():
            fid = self._new_id()
            content = Path(media_body.path).read_bytes() if media_body else None
            if body.get("mimeType") == be.SHEET_MIME:
                content = None
            self.files_db[fid] = {"id": fid, "name": body["name"], "mimeType": body.get(
                "mimeType", media_body.mime if media_body else "x"), "parents": body.get("parents", ["root"]),
                "content": content if body.get("mimeType") != be.FOLDER_MIME else None, "trashed": False}
            self.calls.append(("create", body["name"]))
            return {"id": fid, "md5Checksum": hashlib.md5(content).hexdigest() if content else None}
        return _Req(run)

    def update(self, fileId, media_body=None, body=None, fields=None):
        def run():
            f = self.files_db[fileId]
            if media_body:
                f["content"] = Path(media_body.path).read_bytes()
            if body and body.get("trashed"):
                f["trashed"] = True
            self.calls.append(("update", f["name"]))
            return {"id": fileId, "md5Checksum": hashlib.md5(f["content"] or b"").hexdigest()}
        return _Req(run)

    def get_media(self, fileId):
        return self.files_db[fileId]


class FakeUpload:
    def __init__(self, path, mimetype=None, resumable=False):
        self.path, self.mime = path, mimetype


class FakeDownload:
    def __init__(self, fh, req, chunksize=0):
        self.fh, self.f = fh, req

    def next_chunk(self):
        self.fh.write(self.f["content"])
        return None, True


class FakeSheets:
    def __init__(self):
        self.sheets = {"Sheet1": {"id": 0, "values": []}}
        self.next = 1

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId):
        return _Req(lambda: {"sheets": [{"properties": {"title": t, "sheetId": v["id"]}}
                                        for t, v in self.sheets.items()]})

    def batchUpdate(self, spreadsheetId, body):
        def run():
            for r in body["requests"]:
                if "addSheet" in r:
                    self.sheets[r["addSheet"]["properties"]["title"]] = {"id": self.next, "values": []}
                    self.next += 1
                elif "deleteSheet" in r:
                    sid = r["deleteSheet"]["sheetId"]
                    self.sheets = {k: v for k, v in self.sheets.items() if v["id"] != sid}
                elif "updateSheetProperties" in r:
                    pid = r["updateSheetProperties"]["properties"]["sheetId"]
                    assert any(v["id"] == pid for v in self.sheets.values())
            return {}
        return _Req(run)

    def clear(self, spreadsheetId, range):
        return _Req(lambda: self.sheets[range.strip("'")].update(values=[]))

    def update(self, spreadsheetId, range, valueInputOption, body):
        def run():
            name = range.split("!")[0].strip("'")
            self.sheets[name]["values"] = body["values"]
            import json
            json.dumps(body)  # 必須可序列化
            return {}
        return _Req(run)


@pytest.fixture
def fake(monkeypatch):
    drive, sheets = FakeDrive(), FakeSheets()
    monkeypatch.setattr(googleapiclient.discovery, "build",
                        lambda name, ver, **kw: drive if name == "drive" else sheets)
    monkeypatch.setattr(googleapiclient.http, "MediaFileUpload", FakeUpload)
    monkeypatch.setattr(googleapiclient.http, "MediaIoBaseDownload", FakeDownload)
    for k in ["GOOGLE_REFRESH_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"]:
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv("LOCAL_DRIVE_DIR", raising=False)
    return drive, sheets


def test_gdrive_roundtrip(fake, tmp_path):
    drive, sheets = fake
    b = be.make_backend("台股資料庫")
    assert isinstance(b, be.GDriveBackend)
    root = b.root_id
    # 第二次建立 backend 不會再建一個根資料夾
    b2 = be.GDriveBackend("台股資料庫")
    assert b2.root_id == root
    st = Store(tmp_path / "c1", b)
    st.upsert("price", pd.DataFrame({"date": ["2024-01-02"], "stock_id": ["2330"], "close": [1.0]}))
    st.sync()
    names = {f["name"] for f in drive.files_db.values()}
    assert {"data", "price", "2024.parquet", "meta", "coverage.parquet"} <= names
    # 再寫一次 → update 同一個檔案（不產生重複檔）
    st.upsert("price", pd.DataFrame({"date": ["2024-01-03"], "stock_id": ["2330"], "close": [2.0]}))
    st.sync()
    assert sum(f["name"] == "2024.parquet" for f in drive.files_db.values()) == 1
    assert ("update", "2024.parquet") in drive.calls
    # 全新快取 → 從 Drive 下載
    st2 = Store(tmp_path / "c2", be.GDriveBackend("台股資料庫"))
    assert st2.load("price")["close"].tolist() == [1.0, 2.0]
    # 沒有變動 → 不上傳
    n_calls = len(drive.calls)
    assert st2.sync() == 0
    # 刪除（移到垃圾桶）
    st2.backend.delete("data/price/2024.parquet")
    assert any(f["trashed"] for f in drive.files_db.values() if f["name"] == "2024.parquet")
    assert len(drive.calls) > n_calls


def test_gdrive_sheet(fake, tmp_path):
    from twstock_db import stats, universe
    from twstock_db.config import Settings
    drive, sheets = fake
    st = Store(tmp_path / "c", be.make_backend("台股資料庫"))
    info = pd.DataFrame([{"stock_id": "2330", "stock_name": "台積電", "market": "上市", "kind": "stock",
                          "industry": "", "status": "上市中", "delist_date": ""}])
    st.upsert("price", pd.DataFrame({"date": ["2024-01-02"], "stock_id": ["2330"], "close": [1.0]}))
    st.flush()
    s = stats.compute(st, universe.filter_universe(info, Settings()), Settings())
    stats.write_sheet(st, s)
    assert "Sheet1" not in sheets.sheets
    assert set(sheets.sheets) == {"總覽", "個股覆蓋", "執行紀錄", "資料來源"}
    assert sheets.sheets["總覽"]["values"][1][0] == "日K價量"
    stats.write_sheet(st, s)  # 第二次：沿用同一份試算表
    assert sum(f["mimeType"] == be.SHEET_MIME for f in drive.files_db.values()) == 1
