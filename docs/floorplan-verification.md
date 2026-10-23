# 家的地圖：獨立驗證紀錄

日期：2026-10-23。驗證者未參與功能實作；只新增獨立測試與本紀錄。

結論：核心流程通過，未留下未修正的已確認阻塞問題。範圍是本機家庭服務的平面圖與物品位置，不包含家務排程。

## 自動驗證

執行 `uv run --extra dev pytest -q`：

```text
66 passed, 1 warning in 2.27s
```

唯一 warning 是 FastAPI TestClient 對 httpx 的棄用提醒；沒有測試失敗。`git diff --check` 通過。

`tests/test_floorplan_independent.py` 補上六個獨立案例：

- 兩個 app instance 各自持有 lock，同時保存相同 revision：只有一個 200，另一個 409；兩端讀回同一結果。
- 過期 revision 或無效圖片不改掉現有背景像素、檔案或 revision。
- 登出後重送舊 cookie，地圖、圖片與匯出均為 401／no-store。
- null Origin 與 same-site sibling 的寫入均被拒絕。
- 真 localhost HTTP server 執行 importer 的實際 main 流程：首次匯入成功、沒有 `--replace` 時保護既有圖、明示替換才更新。
- importer 測試使用合成圖片、合成資料及測試密碼，不讀家庭憑證、不呼叫外部服務。

既有測試另涵蓋圖片大小／格式／像素限制、metadata 清除、geometry 邊界與重複參照、CSRF、canonical location 改名、庫存不因取消釘選而刪除，以及 app 重建後持續保存。

## 真瀏覽器流程

同一個 Ego Lite TaskSpace，使用 390×844 與 1280×900 viewport。所有新增資料均有專用 QA 前綴。

| 流程 | 實跑結果 |
| --- | --- |
| 手機導覽與頁面寬度 | 五個導覽入口可用；底部導覽固定；390px 沒有水平溢出 |
| 桌面頁面寬度 | 1280px 沒有水平溢出 |
| 匯入後讀取 | 私有底圖與原有區域載入，重新整理後保留 |
| 房間／標記選取 | 可選取，側欄顯示對應位置與物品 |
| 新增房間、改名、建立新收納標記 | 保存後 API 與重新載入均讀回正確資料 |
| 觸控房間／頂點拖曳 | CDP emulated touch 產生 pointer events，幾何確實改變 |
| 桌面房間／頂點拖曳 | CDP trusted mouse pointer events、buttons 狀態正確，幾何確實改變；只操作未保存的 QA 房間，隨後取消 |
| 跨分頁草稿 | 幾何與 dirty 狀態均保留 |
| 從標記新增物品 | 位置預填正確；零庫存 QA 物品在 Grocy 讀回正確 location_id |
| 搜尋物品與位置 | 選擇結果後正確 pin highlight，顯示對應 QA 物品 |
| 庫存卡「看位置」 | 開啟地圖並選中對應 pin；未釘選位置有明確提示 |
| 既有物品分派 | UI 操作後 API 讀回正確 canonical location_id |
| 跨裝置改名後再分派 | 保留草稿期間另一請求改名；分派仍指向原 location_id，未重建舊名稱的位置 |
| 取消編輯 | 房間草稿撤銷；介面已明示的即時 Grocy 改名保留 |
| 409 衝突 | 另一請求先更新 revision，UI 保存顯示衝突，草稿保留；確認捨棄後讀回最新版 |
| 刪房間與取消釘選 | authenticated API 實跑：移除房間保留 pin 並解除 room_id；取消 pin 保留 Grocy location 與物品 |
| 最終重建／重新載入 | 整合端比較重建前後 plan JSON 相同、圖片 bytes 相同；獨立瀏覽器在重建後重新載入成功 |
| 最終登入／快取 | 地圖與圖片 200／no-store；靜態 app.js 為 no-cache；Service Worker cache 沒有 API 資料 |
| 登出 | 私有地圖 DOM 清空，地圖與圖片均 401 |

## 驗證中發現並修正的問題

1. 舊裝置的編輯草稿可能留著舊 location 名稱。若另一裝置改名，直接用舊名分派會有建立錯誤位置的風險。現在分派前先取得 live locations，再按原 location_id 解析 canonical name；上述真瀏覽器回歸通過。
2. 部署時曾出現新 app.js 與舊 contracts.js 混用，地圖取得未定義 API 而顯示 Not Found。已加入靜態資源 no-cache 與 Service Worker 重新驗證；最終重建後重新載入，API constants 與地圖載入均正確。

## 資料保護與清理

只刪除本次建立的 QA IDs，清理使用最新 revision，沒有覆蓋基準快照。測試房間、標記、零庫存物品與測試收納位置均已移除；原底圖與既有區域保留。桌面滑鼠驗證的最後一個 QA 房間只存在草稿，已取消。

截圖只存於 ignored `.runtime/floorplan-mobile.png` 與 `.runtime/floorplan-desktop.png`，不加入 Git、不在本文件嵌入。文件不記錄私人平面圖、實際房名、位置座標或配置；沒有呼叫 Gemini、Firebase CLI 或任何 cloud 帳號查詢。驗證完成後已登出並關閉測試 TaskSpace。

## 限制

- 390px 為桌面 Chromium 的 viewport 與觸控模擬，沒有實體 iPhone／Android、Safari 或 PWA 安裝驗證。
- 以本機 HTTP 與本機 Grocy 實跑；沒有對外部署或外網／VPN 測試。
- 一般 Ego 原生滑鼠／中文輸入 wrapper 偶有未送達，改用已驗證的 CDP trusted mouse events 與輸入替代方法；不把 wrapper 失敗當成產品通過證據。
- 未執行實際家庭物品的購入、消耗或庫存刪除；只操作本次零庫存 QA 物品。
