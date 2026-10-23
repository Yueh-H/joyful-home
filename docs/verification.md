# 獨立驗證紀錄

驗證日期：2026-10-23，Asia/Taipei。驗證者與實作者為不同 agent。僅使用本專案全新 Grocy 的明標示範資料及 `驗證｜獨立QA品項`，未讀取既有私人家庭資料。

## 結論

本機 Docker 部署與區網 HTTP 手機流程通過本輪驗收。Grocy 是實際庫存後端；Gemini 為真實 `gemini-3-flash-preview` 呼叫。下列模型結果與使用 fake adapter 的單元測試分開記錄。沒有以 fake AI 冒充模型服務。

測試完成後只移除本輪建立的 QA 產品 ID 10，保留原 8 筆 `示範｜` 產品與 volume。最後 state：8 個示範產品、0 個 `驗證｜` 產品、0 個採買項目。示範資料不是使用者真實家庭庫存。

交付前主 agent 再以具明確 fixture 標記的 `seed_demo.py --clear-demo --confirm-demo` 清除這 8 筆自建示範，並清除只屬於 QA ID 10 的測試活動／提案；read-back 為 0 產品、0 採買、0 活動。這是主 agent 的交付整理，不冒充獨立驗證者的操作。執行環境從空家庭開始，下面的截圖保留測試時的示範畫面。

## 真瀏覽器與真 Grocy

使用 ego-browser，單一 TaskSpace 142；工作完成後已 `finish({keep:[]})`。初段測試使用本機 FastAPI，最後必要複驗改用正式 Docker app 與區網網址。

| 檢查 | 實跑結果 |
|---|---|
| 390px / 1280px，今天、庫存、採買、聊天 | 八種組合均 `document.documentElement.scrollWidth === innerWidth`，無橫向溢出 |
| 新增 | 從手機表單新增 ID 10；Grocy read-back 為 quantity 3、unit 瓶、維護間隔 30 天 |
| 編輯 | 初次發現同量盤點會失敗；修正後 Docker/LAN 手機表單成功改型號與效期，quantity 4 保持不變 |
| 消耗 | 手機操作消耗 1；Grocy activity 與 stock log 都有對應紀錄 |
| 採買 | 加入 2 瓶，再填實購 2 完成；庫存新增、採買項目移除 |
| 維護 | 記錄 2026-10-23；30 天週期得到 next_due 2026-11-22 |
| 購入批次開封 | Docker/LAN 手機操作購入 2、效期 2026-12-20、已開封；數量 4→6 |
| 批次隔離 | Grocy entries：舊批次 2/未開封/2026-12-02、新批次 2/已開封/2026-12-20、舊批次 2/未開封/2999；沒有開錯舊批次 |
| 最近批次顯示 | 購入較晚到期的已開封批次後，最近批次仍為 2026-12-02、opened=false |
| LAN HTTP | `isSecureContext=false`、`typeof crypto.randomUUID === 'undefined'`；購入仍成功，驗證 getRandomValues 的 Idempotency-Key fallback |
| 移除保護 | 庫存非零時 UI 移除鈕 disabled，直接 DELETE 亦回 409 |
| 移除與清理 | 表單盤點為零後，移除要求第二次原生 confirm；接受後 ID 10 消失，原 8 示範保留 |

早期兩位 agent 同時使用 QA ID 10，曾出現另一位驗證者的 inventory-correction +1。已由 stock log 確認來源並停止並行寫入；最後 Docker 複驗以穩定 read-back 數量 4 重新開始。未把此測試互擾誤報為消耗或採買缺陷。

## 真 Gemini

| 要求 | 實跑結果 |
|---|---|
| 查詢 QA 品項數量與位置 | 正確回答 5 瓶、驗證測試櫃；沒有 proposal，庫存不變 |
| 用現有食材推薦菜單 | 回答鮮奶與雞蛋的建議；沒有 proposal、沒有扣庫存 |
| 「用掉一些」要求扣庫存 | 追問確切數量；沒有 proposal、庫存不變 |
| 明確消耗 1 瓶 | 先顯示 proposal，確認前庫存不變；點確認才扣 1 |
| Docker 最終複驗 | 確認頁顯示實際品名、物品 #10、位置、操作量 1、單位瓶；390px 無溢出；庫存 6→5 |
| 重送同 proposal | HTTP 409，沒有重複扣庫存 |

預覽品名與數量由 validated actions 產生；server 另產生確定性 summary，前端列出實際 action 明細。防止模型敘述與實際 item_id 不一致的回歸案例使用明確的 FakeAI，收錄於下方單元測試，未宣稱模型實際產生過此錯誤。

## Auth / CSRF / secrets

實際 HTTP 檢查：匿名 `/api/state`、`/api/export` 為 401；缺少 Origin 或 evil Origin 登入為 403；正確登入 200，cookie 有 HttpOnly 與 SameSite=strict；超量消耗 409；不匹配採買 ID 409，拒絕後數量不變；登入後 export 200；登出後 state 401。

`.env` 與 `.local-access.txt` 均為 0600 且 gitignored；`.dockerignore` 排除 env、access 檔、runtime DB 與 Grocy data。驗證過程沒有輸出密碼、API key 或 cookie。PWA service worker 僅快取 shell allowlist，不快取家庭 API 回覆。

## 自動檢查

以下是本輪獨立執行的輸出；後端測試使用明示 FakeGrocy/FakeAI，覆蓋 auth、CSRF、型別/數量、超量、採買關聯、idempotency、stale preview、累積消耗、故障回報、購物撤銷、確定性摘要、最近批次與刪除限制。

```text
uv run pytest -q
24 passed, 1 warning in 5.49s

node --check web/app.js
node --check web/contracts.js
node --check web/sw.js
docker compose config --quiet
all exit 0
```

唯一 warning：Starlette TestClient 的 httpx 相容性 deprecation，非測試失敗。根 agent 另完成 Docker image build、工作樹/容器檔案雜湊比對；這兩項不是本驗證者獨立執行，應以主工作紀錄為證。

## 範圍與限制

未測真實手機硬體、外網 HTTPS、公開反向代理、iOS PWA 安裝/離線、長期多裝置使用或故障注入下的跨服務原子性。390px 是真 Chromium 的 CDP 裝置尺寸模擬。單元測試的故障/rollback 覆蓋不代表真實斷網演練。

測試發現的同量編輯失敗與預覽只信任模型敘述問題均已修正並複驗。本輪沒有剩餘阻塞性 finding。Ego Lite 提示有可用更新；本輪未升級瀏覽器工具。

## 畫面

兩張圖皆為原 8 筆明標示範資料，不含私人家庭資料或 credentials。

- [手機 390px](screenshots/mobile.png)
- [桌面 1280px](screenshots/desktop.png)

## 空家庭補驗（2026-10-23 13:15–13:16；最新結論）

交付清空示範資料後，主 agent 的檢查指令誤用 `/api/shopping_list`，該非清單路徑回 200、JSON null，對 `None` 取 len 導致 TypeError。後續確認正確的 `/api/objects/products` 與 `/api/objects/shopping_list` 均回 200、`[]`。這是檢查指令的路徑錯誤，不能宣稱已重現應用程式的空家庭缺陷；指令已更正。同時新增 `read_list()` 防禦性補強：空 body 正規化為 `[]`，非 list 異常仍報錯；204 分支由新增 mock 測試覆蓋。

補強並重新 build Docker 後，獨立驗證者另以 ego-browser TaskSpace 144、390px 區網 HTTP 完成以下最小流程；沒有重跑整套功能，也沒有重新放回示範資料：

1. 已登入的 `GET /api/state` 回 200，`items=[]`、`shopping=[]`、`grocy.connected=true`。
2. 清除該測試登入 session 後，用家庭密碼從手機登入表單重新登入；首頁顯示「我們的家，從一筆開始」，庫存頁顯示「家的物品，從這裡開始記」。
3. 用表單新增 `驗證｜空家庭204`，取得產品 ID 12，初始數量 0；Grocy state 能正常列出零庫存產品。
4. 編輯該品項，點「移除物品」，接受含完整測試品名的原生第二次確認。移除成功後 `GET /api/state` 仍為 200，items 與 shopping 都回到空陣列，手機庫存頁回到正常空態。
5. TaskSpace 144 已 `finish({keep:[]})`。只移除本輪建立的 ID 12，未觸碰其他未知品項。

本輪實際查詢的 Grocy `objects/products`、`objects/stock`、`objects/shopping_list` 與已移除測試品項 entries 均回 **200，body 為 `[]`（2 bytes）**；沒有在本輪獨立觀察到 204。`Grocy.objects('products')` 與 setup `api('objects/products', env)` 皆得到 `[]`。204/空 body 的防護證據來自新增的明示 mock 案例；不把 200 空集合的實跑描述成真實 204。

```text
uv run pytest -q
27 passed, 1 warning in 0.98s

git diff --check
exit 0
```

最新驗證結論：**27 項測試通過，空家庭 API／手機新增零庫存／確認移除流程通過，無剩餘阻塞 finding**。原 24 項輸出保留作歷史紀錄。新增與移除產生的兩筆活動紀錄 marker 為 `驗證｜空家庭204`；主 agent 已依此 marker 清除兩筆，read-back 為 0 產品、0 採買、0 活動。本驗證者沒有直接修改 runtime DB。

## GitHub CI

首跑 [39904738728](https://github.com/Yueh-H/joyful-home/actions/runs/39904738728) 在準備 action 時失敗：`Unable to resolve action astral-sh/setup-uv@v10`。這是未存在的 major alias；尚未執行應用測試。已查證上游版本 ref，將 checkout 與 setup-uv 固定至對應 commit SHA。

修正後 [39904818152](https://github.com/Yueh-H/joyful-home/actions/runs/39904818152) 成功，包含 27 項測試、JavaScript 語法與 Compose 配置。此紀錄由主 agent 讀取 GitHub run 結果確認。
