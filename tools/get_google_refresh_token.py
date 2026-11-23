"""在你自己的電腦上執行一次，取得 Google Drive 授權碼（refresh token）。

步驟：
  1. pip install google-auth-oauthlib
  2. 把 Google Cloud 下載的 OAuth 用戶端 JSON 存成 client_secret.json，放在這個檔案旁邊
  3. python get_google_refresh_token.py
  4. 瀏覽器會打開 Google 登入頁 → 選你的帳號 → 允許
  5. 畫面會印出三個值，分別貼到 GitHub Secrets：
       GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN

權限只有 drive.file：程式只能看到、修改「它自己建立的檔案」，碰不到你雲端硬碟裡的其他檔案。
"""
import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main():
    path = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("client_secret.json"))
    if not path.exists():
        print(f"找不到 {path}，請先從 Google Cloud 下載 OAuth 用戶端 JSON。")
        sys.exit(1)
    flow = InstalledAppFlow.from_client_secrets_file(str(path), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    info = json.loads(path.read_text())
    cfg = info.get("installed") or info.get("web") or {}
    print("\n===== 請把下面三個值存到 GitHub → Settings → Secrets and variables → Actions =====\n")
    print("GOOGLE_CLIENT_ID     =", cfg.get("client_id"))
    print("GOOGLE_CLIENT_SECRET =", cfg.get("client_secret"))
    print("GOOGLE_REFRESH_TOKEN =", creds.refresh_token)
    print("\n（這三個值等同你的 Google 雲端硬碟鑰匙，不要貼給別人或放到程式碼裡）")


if __name__ == "__main__":
    main()
