# 台股歷史資料庫爬蟲 agent

每小時在 GitHub 上自動執行（你的電腦、網頁都不用開），一直抓台股歷史資料直到補齊，
之後每天自動更新，資料存在你的 **Google 雲端硬碟「台股資料庫」資料夾**。

- 範圍：上市、上櫃、興櫃、ETF、已下市股票，2010-01-01 起
- 統計介面：Google 試算表「資料庫統計」＋ 網頁儀表板 `https://qwert0426.github.io/Jackylin/`
- 想改抓什麼、從哪年開始：改 [`crawler_config.yaml`](crawler_config.yaml)

## 抓哪些資料、從哪裡抓

| 資料 | 每日更新來源（全市場一次抓） | 歷史回補來源（逐檔） |
|---|---|---|
| 日K價量 | 證交所 MI_INDEX、櫃買 dailyQuotes | FinMind TaiwanStockPrice |
| 本益比／淨值比／殖利率 | 證交所 BWIBBU_d、櫃買 peQryDate | FinMind TaiwanStockPER |
| 三大法人買賣超 | 證交所 T86、櫃買 insti/dailyTrade | FinMind TaiwanStockInstitutionalInvestorsBuySell |
| 融資融券餘額 | 證交所 MI_MARGN、櫃買 margin/balance | FinMind TaiwanStockMarginPurchaseShortSale |
| 外資持股比例 | 證交所 MI_QFIIS、櫃買 insti/qfii | FinMind TaiwanStockShareholding |
| 借券賣出餘額 | 證交所 TWT93U、櫃買 margin/sbl | FinMind TaiwanDailyShortSaleBalances |
| 當沖成交量／金額 | 證交所 TWTB4U、櫃買 intraday/stat | FinMind TaiwanStockDayTrading |
| 月營收 | — | FinMind TaiwanStockMonthRevenue |
| 綜合損益表／資產負債表／現金流量表 | — | FinMind FinancialStatements / BalanceSheet / CashFlowsStatement |
| 除權息結果、減資參考價 | — | FinMind DividendResult / CapitalReductionReferencePrice |
| 集保股權分散（大戶持股） | 集保開放資料（每週全市場） | 集保個股查詢頁（最近約一年） |
| EPS、毛利率、ROE、還原股價、400 張大戶比例 | 由上面資料計算 | |

網址：證交所 https://www.twse.com.tw ・ 櫃買中心 https://www.tpex.org.tw ・
FinMind https://finmindtrade.com ・ 集保 https://www.tdcc.com.tw/portal/zh/smWeb/qryStock

> 大戶持股：集保官方免費資料只能查到最近約一年。更早的歷史只有付費來源（FinMind Sponsor、TEJ）。
> 如果之後升級 FinMind 付費，把設定檔 `finmind_sponsor` 改成 `true`，就會自動補齊 2010 年起的資料。

## 雲端硬碟裡的檔案

```
台股資料庫/
├─ 資料庫統計                ← Google 試算表：總覽、個股覆蓋、執行紀錄、資料來源（手機直接看）
├─ 個股Excel/上市/2330_台積電.xlsx  ← 每檔一個 Excel：日K(含還原價)、估值、法人、融資券、月營收、財報季…
├─ data/price/2024.parquet   ← 給程式／回測用（每個資料集、每年一個檔，所有股票都在裡面）
└─ meta/                     ← 進度、覆蓋率、執行紀錄（程式自己用）
```

Parquet 欄位名稱和 FinMind 文件相同（例如日K：`date, stock_id, open, max, min, close, Trading_Volume …`）。

寫選股／回測程式時：

```python
from twstock_db.reader import Database
db = Database("G:/我的雲端硬碟/台股資料庫")     # 電腦裝 Google 雲端硬碟後的資料夾
px = db.price(["2330"], start="2020-01-01")    # 含還原股價 adj_close
ratios = db.ratios(["2330"])                   # EPS、毛利率、ROE
```

## 安裝步驟（只要做一次）

### 第 1 步：拿 FinMind 鑰匙
1. 打開 https://finmindtrade.com ，點右上角「登入」，用 Google 帳號登入。
2. 登入後點右上角你的名字 →「個人資訊」。
3. 看到一長串英文數字的「API Token」，按複製。先貼在記事本。

### 第 2 步：建立 Google 授權（手機或電腦瀏覽器都可以，不用裝任何程式）
（「授權」= 允許這支程式把檔案放進你的雲端硬碟。手機建議點瀏覽器選單的「電腦版網站」，按鈕比較好找。）

**2-1 開一個專案**
1. 打開 https://console.cloud.google.com/projectcreate ，用你的 Gmail 登入。
2. 「專案名稱」打 `twstock` → 按「建立」。
3. 等右上角鈴鐺出現「已建立專案」。之後每一頁的最上方都要確認選的是 `twstock`。

**2-2 打開兩個功能（API）**
1. 打開 https://console.cloud.google.com/apis/library/drive.googleapis.com → 按藍色「啟用」。
2. 打開 https://console.cloud.google.com/apis/library/sheets.googleapis.com → 按藍色「啟用」。
3. 兩頁都看到「API 已啟用」或「管理」按鈕，就代表成功。

**2-3 設定同意畫面**
1. 打開 https://console.cloud.google.com/auth/overview →「開始使用」。
2. 「應用程式名稱」打 `twstock`，「使用者支援電子郵件」選你的 Gmail →「下一步」。
3. 「目標對象」選「外部」→「下一步」。
4. 「聯絡資訊」打你的 Gmail →「下一步」→ 勾選同意 →「建立」。
5. 打開 https://console.cloud.google.com/auth/audience → 找到「發布狀態：測試中」→ 按「發布應用程式」→「確認」。
   看到「發布狀態：正式版」就對了。
   **這步一定要做**：沒有按的話，授權 7 天後就會失效，爬蟲會停。

**2-4 建立鑰匙（OAuth 用戶端）**
1. 打開 https://console.cloud.google.com/auth/clients/create
2. 「應用程式類型」選「**網頁應用程式**」，名稱打 `twstock`。
3. 往下找「已授權的重新導向 URI」→ 按「新增 URI」→ 貼上這一行（一個字都不能錯）：
   `https://developers.google.com/oauthplayground`
4. 按「建立」。
5. 跳出的視窗有「用戶端 ID」和「用戶端密鑰」，兩個都按旁邊的複製圖示，貼到記事本。
   （關掉後也可以在 https://console.cloud.google.com/auth/clients 點 `twstock` 再看到）

### 第 3 步：用網頁拿授權碼（Refresh token，只要一次）
1. 打開 https://developers.google.com/oauthplayground
2. 按右上角的**齒輪圖示** ⚙️ → 勾選「Use your own OAuth credentials」。
3. 下面出現兩個框：「OAuth Client ID」貼用戶端 ID、「OAuth Client secret」貼用戶端密鑰。按空白處關掉齒輪視窗。
4. 左邊「Step 1」最下面有一個輸入框（寫著 Input your own scopes），貼上：
   `https://www.googleapis.com/auth/drive.file`
5. 按藍色「Authorize APIs」→ 選你的 Gmail。
6. 如果出現「Google 尚未驗證這個應用程式」：點「進階」→「前往 twstock（不安全）」→「繼續」。
   （這是你自己建立的程式，所以 Google 還沒審查，可以放心。權限只有「本程式自己建立的檔案」，碰不到你其他檔案。）
7. 回到 Playground，左邊會自動跳到「Step 2」→ 按藍色「Exchange authorization code for tokens」。
8. 下面出現「Refresh token」（`1//` 開頭的一長串），複製貼到記事本。
9. 現在記事本裡應該有 3 個值：用戶端 ID、用戶端密鑰、Refresh token。

> 電腦上會用 Python 的人，也可以改用 `tools/get_google_refresh_token.py`（2-4 的類型要改選「電腦版應用程式」）。

### 第 4 步：把 4 把鑰匙放進 GitHub
1. 打開 https://github.com/qwert0426/Jackylin/settings/secrets/actions
2. 按綠色按鈕「New repository secret」。
3. Name 打 `FINMIND_TOKEN`，Secret 貼第 1 步的 Token →「Add secret」。
4. 同樣方法再新增三個：
   - `GOOGLE_CLIENT_ID` ← 貼「用戶端 ID」
   - `GOOGLE_CLIENT_SECRET` ← 貼「用戶端密鑰」
   - `GOOGLE_REFRESH_TOKEN` ← 貼「Refresh token」
5. 完成後清單裡應該看到 4 個名字（看不到內容是正常的，代表有保護好）。

### 第 5 步：打開網頁儀表板
1. 打開 https://github.com/qwert0426/Jackylin/settings/pages
2. 「Build and deployment」下面的「Source」選「GitHub Actions」（選了就自動儲存）。

### 第 6 步：啟動
1. 上方點「Actions」。如果看到「I understand my workflows…」的綠色按鈕，按下去。
2. 左邊點「台股資料庫爬蟲」→ 右邊「Run workflow」→ 綠色「Run workflow」。
3. 等大約 1 分鐘，畫面會出現一個黃色轉圈的執行項目；大約 50 分鐘後變成綠色勾勾 ✅。
4. 打開你的 Google 雲端硬碟，會看到「台股資料庫」資料夾和「資料庫統計」試算表。
5. 打開 https://qwert0426.github.io/Jackylin/ 會看到像截圖那樣的進度條。

之後每小時會自己跑，不用再做任何事。

## 多久會補齊？
- 每日資料：官方來源，每天晚上就會是最新。
- 歷史回補：約 3,500 檔 × 13 類資料 ≈ 3.5 萬次 FinMind 請求；免費額度每小時 600 次，
  **約 3～4 天**補完。若同一組 FinMind Token 也被其他程式使用，兩邊會搶額度，時間會拉長。
- 大戶持股過去一年：集保一次只能查一檔一週，約 1～2 週補完。

## 開發者
- 測試：`python -m pytest tests -q`；本機端對端測試：`python tests/run_local_e2e.py /tmp/x`
- 用本機資料夾代替 Google Drive：設環境變數 `LOCAL_DRIVE_DIR=/某個資料夾`
