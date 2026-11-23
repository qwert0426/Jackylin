"""雲端儲存後端：Google Drive（正式）與本機資料夾（測試 / 搭配 Google Drive 電腦版）。

路徑一律用相對路徑字串，例如 'data/price/2024.parquet'。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

from ..util import log

FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class LocalBackend:
    """把一個本機資料夾當成雲端硬碟。"""

    kind = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def list_all(self) -> dict:
        out = {}
        for p in self.root.rglob("*"):
            if p.is_file():
                rel = p.relative_to(self.root).as_posix()
                out[rel] = {"id": rel, "md5": md5_file(p), "size": p.stat().st_size}
        return out

    def download(self, rel: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.root / rel, dest)

    def upload(self, src: Path, rel: str, mime: str | None = None) -> str:
        dst = self.root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return rel

    def delete(self, rel: str) -> None:
        p = self.root / rel
        if p.exists():
            p.unlink()

    def folder_url(self) -> str:
        return str(self.root)


class GDriveBackend:
    """Google Drive（OAuth refresh token，scope = drive.file：只能看到本程式建立的檔案）。"""

    kind = "gdrive"

    def __init__(self, folder_name: str):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        self.creds = creds
        self.drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.folder_name = folder_name
        self.root_id = self._find_or_create_root()
        self._folders: dict[str, str] = {"": self.root_id}
        self._files: dict[str, dict] = {}

    # ---------------------------------------------------------------- helpers
    def _retry(self, fn, tries: int = 5):
        for i in range(tries):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                if i == tries - 1:
                    raise
                log.warning("Google API 錯誤，重試 %d：%s", i + 1, e)
                time.sleep(2 ** i * 2)

    def _find_or_create_root(self) -> str:
        q = (f"name = '{self.folder_name}' and mimeType = '{FOLDER_MIME}' and trashed = false "
             f"and 'root' in parents")
        res = self._retry(lambda: self.drive.files().list(q=q, fields="files(id)").execute())
        if res.get("files"):
            return res["files"][0]["id"]
        meta = {"name": self.folder_name, "mimeType": FOLDER_MIME}
        f = self._retry(lambda: self.drive.files().create(body=meta, fields="id").execute())
        log.info("已在 Google Drive 建立資料夾「%s」", self.folder_name)
        return f["id"]

    def _children(self, folder_id: str) -> list:
        items, token = [], None
        while True:
            res = self._retry(lambda: self.drive.files().list(
                q=f"'{folder_id}' in parents and trashed = false", pageSize=1000, pageToken=token,
                fields="nextPageToken, files(id, name, mimeType, md5Checksum, size)").execute())
            items += res.get("files", [])
            token = res.get("nextPageToken")
            if not token:
                return items

    def _folder(self, rel_dir: str) -> str:
        if rel_dir in self._folders:
            return self._folders[rel_dir]
        parent_rel, _, name = rel_dir.rpartition("/")
        parent = self._folder(parent_rel)
        safe = name.replace("'", "\\'")
        q = (f"name = '{safe}' and mimeType = '{FOLDER_MIME}' and trashed = false "
             f"and '{parent}' in parents")
        res = self._retry(lambda: self.drive.files().list(q=q, fields="files(id)").execute())
        if res.get("files"):
            fid = res["files"][0]["id"]
        else:
            body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}
            fid = self._retry(lambda: self.drive.files().create(body=body, fields="id").execute())["id"]
        self._folders[rel_dir] = fid
        return fid

    # ---------------------------------------------------------------- API
    def list_all(self) -> dict:
        out: dict[str, dict] = {}
        stack = [("", self.root_id)]
        while stack:
            rel_dir, fid = stack.pop()
            for f in self._children(fid):
                rel = f"{rel_dir}/{f['name']}" if rel_dir else f["name"]
                if f["mimeType"] == FOLDER_MIME:
                    self._folders[rel] = f["id"]
                    stack.append((rel, f["id"]))
                else:
                    out[rel] = {"id": f["id"], "md5": f.get("md5Checksum"),
                                "size": int(f.get("size", 0) or 0), "mime": f["mimeType"]}
        self._files = out
        return out

    def download(self, rel: str, dest: Path) -> None:
        from googleapiclient.http import MediaIoBaseDownload

        fid = self._files[rel]["id"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")

        def _do():
            with open(tmp, "wb") as fh:
                dl = MediaIoBaseDownload(fh, self.drive.files().get_media(fileId=fid),
                                         chunksize=32 << 20)
                done = False
                while not done:
                    _, done = dl.next_chunk()
        self._retry(_do)
        tmp.replace(dest)

    def upload(self, src: Path, rel: str, mime: str | None = None) -> str:
        from googleapiclient.http import MediaFileUpload

        rel_dir, _, name = rel.rpartition("/")
        media = MediaFileUpload(str(src), mimetype=mime or "application/octet-stream",
                                resumable=src.stat().st_size > 5 << 20)
        existing = self._files.get(rel)
        if existing:
            f = self._retry(lambda: self.drive.files().update(
                fileId=existing["id"], media_body=media, fields="id, md5Checksum").execute())
        else:
            body = {"name": name, "parents": [self._folder(rel_dir)]}
            f = self._retry(lambda: self.drive.files().create(
                body=body, media_body=media, fields="id, md5Checksum").execute())
        self._files.setdefault(rel, {}).update(id=f["id"], md5=f.get("md5Checksum"),
                                               size=src.stat().st_size)
        return f["id"]

    def delete(self, rel: str) -> None:
        f = self._files.pop(rel, None)
        if f:
            self._retry(lambda: self.drive.files().update(
                fileId=f["id"], body={"trashed": True}).execute())

    def find_or_create_sheet(self, name: str) -> str:
        for rel, f in self._files.items():
            if rel == name and f.get("mime") == SHEET_MIME:
                return f["id"]
        body = {"name": name, "mimeType": SHEET_MIME, "parents": [self.root_id]}
        f = self._retry(lambda: self.drive.files().create(body=body, fields="id").execute())
        self._files.setdefault(name, {}).update(id=f["id"], md5=None, size=0, mime=SHEET_MIME)
        return f["id"]

    def folder_url(self) -> str:
        return f"https://drive.google.com/drive/folders/{self.root_id}"


def make_backend(folder_name: str, local_dir: str | None = None):
    """有 Google 憑證就用 Drive；否則（或指定 LOCAL_DRIVE_DIR）用本機資料夾。"""
    local_dir = local_dir or os.environ.get("LOCAL_DRIVE_DIR")
    if local_dir:
        return LocalBackend(local_dir)
    if os.environ.get("GOOGLE_REFRESH_TOKEN"):
        return GDriveBackend(folder_name)
    raise RuntimeError("沒有設定 GOOGLE_REFRESH_TOKEN（Google Drive）也沒有設定 LOCAL_DRIVE_DIR")
