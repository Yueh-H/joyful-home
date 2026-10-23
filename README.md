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
       ├── 本機 MongoDB → 庫存、效期、位置、採買與新版地圖配置
       └── Gemini API → 中文理解、資料查詢、操作提案
```

正式家庭環境以本機 MongoDB 為唯一業務資料來源：`INVENTORY_BACKEND=mongodb`。庫存、位置、採買與新版地圖配置存於 `joyful_home.households` 同一份家庭文件；兩個網頁服務共用它，沒有背景 Firebase 同步。登入、提案與操作歷史仍保留在原 app-data 的本機 SQLite，模型保留在原 map-data；切換不會重設家庭密碼或 AI 金鑰。舊 Firestore／[Grocy](https://github.com/grocy/grocy) adapter 與資料保留供明確回復；本機資料缺少或讀取失敗會回報錯誤，不偷偷回到舊庫。

AI 回答會取得當下庫存與日期；未知的數量、位置或效期應補問。推薦食譜不會自動扣庫存。資料庫只在私有 Docker network 供後端讀寫，不發布主機 DB 連接埠；Gemini API 金鑰只在後端。瀏覽器不能直接連私人資料庫。

### 用地圖找物品

原網址 `/#map` 顯示新版向量家具地圖，家庭管家的原選單仍保留。點家具即可查看內容，第一次可連結既有庫存位置，之後直接新增、編輯或移動裡面的物品。「編輯配置」只處理家具位置、大小與屬性，完成後回到瀏覽模式；移除家具圖示會保留庫存。

從庫存點「看位置」會定位到綁定該位置的家具。多人編輯衝突會保留草稿；背景更新不會重建地圖或清掉草稿。

新版地圖服務的原始碼保留在相鄰 `../joyful-home-map` 資料夾。先啟動本家庭服務，再於該資料夾執行 `uv run python scripts/start.py`；家庭管家透過同源路徑 `/map-studio/` 載入它，日常仍使用 8765 的同一個網址。未啟動地圖服務時，其他家庭功能仍可使用。

新版家具配置與庫存存於同一個本機 MongoDB 家庭文件，不放進公開 repo，也不傳給 Gemini。原始參考底圖仍保留在本機私人 volume，由舊底圖 API 提供參考圖；正式地圖使用該文件的向量配置。舊圖釘介面已被新版替換。

## 本機啟動

需要 Docker Compose；已設定並完成資料匯入的本機家庭環境可直接啟動：

```bash
docker compose up -d --build mongo app
```

再到相鄰地圖資料夾執行 `uv run python scripts/start.py`。MongoDB 使用專用 `joyful-home_mongo-data` volume，僅在 `joyful-home_default` network 內提供 `mongo:27017`；不會使用或修改這台電腦上其他 MongoDB。不要刪除 MongoDB、app-data 或 map-data volumes。

全新環境以 `.env.example` 設定家庭密碼、至少 32 字的 session secret 及本機 MongoDB 參數；已有 `.env` 請保留內容，不重新執行 Grocy 初始化。從舊家庭搬遷必須先備份、驗證 JSON DTO 並明確匯入空的專用 MongoDB，再切換模式；完整步驟及匯出見 [本機資料搬遷](docs/local-storage.md)。匯入會保留物品、位置、家具與分格 ID；不依舊雲端故障自動建立空家庭。

Firestore 仍可作明確回復模式：加入 `-f compose.yaml -f compose.firestore.yaml`，沿用原私有憑證 volume，設定過程見舊 [Firestore 設定](docs/firestore-setup.md)。Grocy 相容模式透過 `--profile legacy` 與 `INVENTORY_BACKEND=grocy` 明確啟動；一般本機模式不需要它或 Firebase 憑證。切換模式不會刪掉舊資料，亦不會將本機變更自動回寫雲端。

開發時可用 [uv](https://docs.astral.sh/uv/) 執行 Python；主機上的 MongoDB URI 必須指向自己為此專案提供的開發連線，不能直接假設 `localhost:27017` 就是這個家庭資料庫。容器中的 `mongodb://mongo:27017` 不等於主機位址。

在電腦開啟 `http://localhost:8765`。手機與電腦在同一個 Wi-Fi 時，使用電腦的區網 IP，例如 `http://192.168.1.100:8765`。伺服器和 Docker 必須保持執行；localhost 只代表開啟網頁的那部裝置。

### 啟用自然語言

後端環境需要 `GEMINI_API_KEY`，模型可用 `GEMINI_MODEL` 調整。可透過執行環境或本機 `.env` 設定；以 `.env.example` 為欄位參考。請使用自己有權使用的 API 金鑰。

沒有金鑰時，物品管理與採買仍可使用；聊天介面會顯示尚未啟用。使用 Gemini 時，當下家庭庫存、採買與近期操作紀錄會隨問題及對話脈絡傳至該服務，API 使用可能產生費用。

### 示範資料

以下示範腳本是舊 Grocy 測試工具，只在明確的隔離 Grocy 模式使用，不拿來初始化或清空正式 MongoDB 家庭。測試時可加入可辨識的示範物品：

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

- 兩部手機登入同一個家庭服務，即讀取同一份本機 MongoDB 家庭資料。
- 網頁會重新載入最新資料；需要網路連線，不能離線修改庫存。
- 安裝到主畫面與 service worker 需要 HTTPS（或本機 localhost）；一般區網 HTTP 仍能瀏覽使用。
- 要在外面或全天使用，請將服務放在常開主機 / NAS，設定 HTTPS 與家庭登入；也可以經由自己的私人網路連回家。
- 本專案初版供單一家庭使用，並未提供多租戶帳號系統。
- 初版提醒顯示在網頁內；關閉網頁後的手機推播尚未提供。
- 同一物品可有不同批次效期；總覽顯示最早效期，編輯效期／開封狀態作用於最近效期批次。補貨時可填新批次效期。

## 開源與私人資料

本專案以 MIT 授權公開程式碼。家庭資料、API 金鑰、登入密碼、Docker volumes 和執行紀錄不屬於公開 repo。

GitHub 用於原始碼和版本管理；GitHub Pages 的靜態服務不能單獨執行這個後端。

「問問家庭管家」的文字對話另存於同一個本機 MongoDB 的 `chat_turns` collection，順序由 `chat_sequences` 的家庭序號保存，不會膨脹庫存／地圖文件。登入同一家庭即可在重整、重開或另一個瀏覽器讀取；「載入更早的對話」可逐頁回看，沒有自動刪除歷史。每次送出會先保存訊息，再產生與保存回覆；網路或資料庫錯誤會標示保存尚未確認。相同訊息編號重送不會重複呼叫 AI。中斷的回覆最多保留 120 秒處理中狀態，之後可重新提出要求，不會自動重送增加費用。AI 只讀最近 16 則已完成對話作上下文，歷史提案只供回看，必須重新提出要求才有新的確認介面。升級前只在舊頁記憶體的內容若已消失，無法回復；目前聊天只支援文字。對話備份須包含整個 MongoDB volume 或額外匯出 `chat_turns` 與 `chat_sequences`；既有庫存／地圖 JSON 匯出不包含對話。

正式物品、位置、採買與家具配置保存在本機 MongoDB 的 `joyful-home_mongo-data` volume，容器重啟不會清空。登入、提案與操作紀錄仍在本機 `app-data`，模型與原始參考圖仍在 `map-data`；完整備份須一併保存這些 volumes、私人設定與完整 JSON DTO。舊 Firebase 和 Grocy 資料保留，不再接收正式寫入。目前由這台 Mac 提供 Wi-Fi 網頁，Mac／Docker 停止時手機無法連線。

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
