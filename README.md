# 愉悅的家庭管家 · Joyful Home

給兩個人一起使用的家庭物品管理網頁。先照顧食材與家用品：知道家裡有什麼、放在哪裡、該買什麼，以及何時需要清潔或更換。

手機和電腦使用相同網址、同一份家庭資料。**阿魚是家人，AI 助理稱為「管家」。**

## 日常使用

- **今天**：查看快到期食材、低庫存用品與維護日期。
- **家裡有什麼**：搜尋、分類、新增及編輯物品，記錄購買、消耗、位置與規格。
- **家的地圖**：同一個頁面使用新版家具地圖；點家具整理內容，編輯配置時移動家具與調整大小。
- **採買**：共用食材與家用品清單，買完依實際數量入庫。
- **問管家**：用繁體中文查詢與整理家庭紀錄；寫入前顯示辨識結果，再一鍵套用。

自然語言範例：

> 家裡還有多少牛奶？
>
> 買了兩瓶洗衣精，放在陽台櫃子。
>
> 今天晚餐可以做什麼？優先用快到期的食材。
>
> 延長線放在哪裡？
>
> 今天換了廚下濾芯。

桌面畫面（僅使用明確標示的示範資料）：

![愉悅的家庭管家桌面版](docs/screenshots/desktop.png)

[手機版畫面](docs/screenshots/mobile.png)

## 架構

```text
手機 / 電腦瀏覽器
       │
       ▼
愉悅的家庭管家（FastAPI + 原生 HTML / CSS / JavaScript）
       ├── Firestore → 庫存、效期、位置、採買與新版地圖配置
       └── Gemini API → 中文理解、資料查詢、操作提案
```

正式家庭環境以 Firestore 為唯一資料來源：`INVENTORY_BACKEND=firestore`。庫存、位置、採買和新版家具配置都直接讀寫同一個 Firebase 專案，不在 Grocy 另外同步一份。舊 [Grocy](https://github.com/grocy/grocy) adapter 保留相容與搬遷用途；雲端連線失敗會回報錯誤，不回寫舊資料庫。

AI 回答會取得當下庫存與日期；未知的數量、位置或效期應補問。推薦食譜不會自動扣庫存。Firebase 服務憑證與 Gemini API 金鑰只在後端，瀏覽器不能直接讀寫私人資料庫。

### 用地圖找物品

原網址 `/#map` 顯示新版向量家具地圖，家庭管家的原選單仍保留。點家具即可查看內容，第一次可連結既有庫存位置，之後直接新增、編輯或移動裡面的物品。「編輯配置」只處理家具位置、大小與屬性，完成後回到瀏覽模式；移除家具圖示會保留庫存。

從庫存點「看位置」會定位到綁定該位置的家具。多人編輯衝突會保留草稿；背景更新不會重建地圖或清掉草稿。

新版地圖服務的原始碼保留在相鄰 `../joyful-home-map` 資料夾。先啟動本家庭服務，再於該資料夾執行 `uv run python scripts/start.py`；家庭管家透過同源路徑 `/map-studio/` 載入它，日常仍使用 8765 的同一個網址。未啟動地圖服務時，其他家庭功能仍可使用。

新版家具配置存於 Firestore，不放進公開 repo，也不傳給 Gemini。原始參考底圖仍保留在本機私人 volume，由舊底圖 API 提供參考圖；正式地圖使用雲端的向量配置。舊圖釘介面已被新版替換。

## 本機啟動

已設定的 Firestore 家庭環境直接啟動 `docker compose --profile app up -d --build app`，再於相鄰地圖資料夾執行啟動腳本。需保留私人 `firebase-credentials` volume；不要重新執行 Grocy 初始化。新環境的設定、一次性搬遷和資料範圍見 [Firestore 設定](docs/firestore-setup.md)。

以下是舊 Grocy 相容模式的首次建立流程：

需要 Docker Compose 與 Python 3；應用程式本身在容器內使用 Python 3.12。

```bash
git clone https://github.com/Yueh-H/joyful-home.git
cd joyful-home
python3 scripts/setup_grocy.py
docker compose --profile app up -d --build
```

設定腳本初始化此專案專用的 Grocy 服務與本機設定。登入資訊保存在忽略版控的 `.local-access.txt`，請從該檔取得家庭密碼。請勿把這個檔案或 `.env` 上傳至 GitHub。

開發時也可使用主機上的 Python 3.12+ 與 [uv](https://docs.astral.sh/uv/)：先停止 `app` 容器以釋放連接埠，保留 Grocy，再執行：

```bash
docker compose stop app
uv sync --extra dev
uv run uvicorn server.app:app --host 0.0.0.0 --port 8765
```

在電腦開啟 `http://localhost:8765`。手機與電腦在同一個 Wi-Fi 時，使用電腦的區網 IP，例如 `http://192.168.1.100:8765`。伺服器和 Docker 必須保持執行；localhost 只代表開啟網頁的那部裝置。

### 啟用自然語言

後端環境需要 `GEMINI_API_KEY`，模型可用 `GEMINI_MODEL` 調整。可透過執行環境或本機 `.env` 設定；以 `.env.example` 為欄位參考。請使用自己有權使用的 API 金鑰。

沒有金鑰時，物品管理與採買仍可使用；聊天介面會顯示尚未啟用。使用 Gemini 時，當下家庭庫存、採買與近期操作紀錄會隨問題及對話脈絡傳至該服務，API 使用可能產生費用。

### 示範資料

正式使用從空資料開始。測試時可明確加入可辨識的示範物品：

```bash
uv sync --extra dev
uv run python scripts/seed_demo.py --confirm-demo
```

示範資料不是家中真實庫存。匯入前請閱讀腳本的執行提示，不要把測試盤點當成實際採買紀錄。

僅在資料仍全部為這個腳本的示範品項時，可以明確清除它們，保留服務設定：

```bash
uv run python scripts/seed_demo.py --clear-demo --confirm-demo
```

若已有任何真實品項，腳本會拒絕清除。請先在另一個測試環境試用，或在開始登錄真實庫存前完成示範。

## 手機與家庭共用

- 兩部手機登入同一個家庭服務，即讀取同一份 Firestore 家庭資料。
- 網頁會重新載入最新資料；需要網路連線，不能離線修改庫存。
- 安裝到主畫面與 service worker 需要 HTTPS（或本機 localhost）；一般區網 HTTP 仍能瀏覽使用。
- 要在外面或全天使用，請將服務放在常開主機 / NAS，設定 HTTPS 與家庭登入；也可以經由自己的私人網路連回家。
- 本專案初版供單一家庭使用，並未提供多租戶帳號系統。
- 初版提醒顯示在網頁內；關閉網頁後的手機推播尚未提供。
- 同一物品可有不同批次效期；總覽顯示最早效期，編輯效期／開封狀態作用於最近效期批次。補貨時可填新批次效期。

## 開源與私人資料

本專案以 MIT 授權公開程式碼。家庭資料、API 金鑰、登入密碼、Docker volumes 和執行紀錄不屬於公開 repo。

GitHub 用於原始碼和版本管理；GitHub Pages 的靜態服務不能單獨執行這個後端。

正式物品、位置、採買與家具配置保存在 Firestore，容器重啟不會清空。登入、提案與操作紀錄仍在本機 `app-data` volume，原始參考底圖也在本機；完整備份須另保存這些資料與私人設定／憑證。舊 Grocy 和 SQLite volumes 保留作搬遷前備份，不再接收正式寫入。資料庫上雲不等於網頁已部署，現在仍由這台 Mac 提供 Wi-Fi 網頁。

## 驗證

```bash
uv run pytest -q
node --check web/app.js
node --check web/contracts.js
node --check web/floorplan.js
node --check web/sw.js
docker compose config --quiet
```

實際執行結果、瀏覽器檢查與尚未完成項目見 [驗證紀錄](docs/verification.md)。API 及資料欄位見 [開發契約](docs/contract.md)。

新版整合見 [整合契約](docs/map-studio-integration.md) 與 [整合驗證](docs/map-integration-verification.md)。原底圖 API 的格式與歷史實測保留於 [地圖契約](docs/floorplan-contract.md) 和 [地圖驗證](docs/floorplan-verification.md)。

## 上游與授權

- [Grocy 原始碼](https://github.com/grocy/grocy) / [官方功能](https://grocy.info/)
- [Gemini structured outputs](https://ai.google.dev/gemini-api/docs/structured-output)
- [MIT License](LICENSE) / [第三方說明](THIRD_PARTY.md)
