# 本地 MongoDB 資料與匯入

`INVENTORY_BACKEND=mongodb` 選用本機 MongoDB，`mongo` 是相容別名；API 回報的 backend 固定為 `mongodb`。庫存與新版地圖共用 `joyful_home.households` 中的 `_id: home` 文件。`MONGODB_URI`、`MONGODB_DATABASE`、`HOME_ID` 可由後端設定。部署與 volume 說明見 [本機 MongoDB 部署說明](../README.md)。

文件保存 `schema_version:1`、共用 `version`、`inventory` 與 `layout`。`inventory` 的 items／locations／shopping 以十進位整數 ID 字串為 key；`counters` 保存各類**下一個**可用 ID。API 與匯出 DTO 仍使用陣列和整數 ID。資料不存在時明確回報 503，不自動建立空家庭或改讀 Firebase／Grocy。

每次庫存寫入重新讀取文件，驗證批次與數量，再以 `_id/version` 做條件更新；只 `$set inventory`、`$inc version`，不替換整份文件。CAS 衝突最多重試五次。採買完成的入庫和移除採買列在同一次文件更新中完成；驗證失敗不會部分寫入。地圖由原地圖服務保存同一文件的 layout，保持自己的 revision 防衝突。Mongo 寫入要求 `w=1,j=True`；這不是跨機器備援。

## 匯入已驗證快照

工具不查 Firebase；只接受明確提供的私人 JSON。快照格式為 `{state:{items:[],shopping:[]},locations:[...],layout:{...}}`，亦接受既有 `after.json` 的 `{locations:{locations:[...]}}`。用 companion 的 `server.py` AST 載入正式 Layout schema，不啟動地圖 app 或舊資料庫。

```sh
python scripts/mongo_household.py validate --approved-home \
  --snapshot /private/after.json --layout-schema /private/map-server.py

python scripts/mongo_household.py import --approved-home \
  --snapshot /private/after.json --layout-schema /private/map-server.py
```

`--approved-home` 強制此批搬遷基準：revision 37、97 個位置、35 件家具、76 格、0 品項、0 採買。匯入完整保留 ID；新增位置依已保存的 next-ID counter 配置編號。只在目的資料庫空白時做一次完整 insert；匯入 SHA 和內容都相同時可重跑驗證，目的資料不同或匯入後已修改會拒絕覆寫。匯入前須備份原設定、runtime、模型與私人快照。CLI 不會變更原帳密、Gemini 金鑰、雲端資料或模型。

## 完整 JSON DTO 匯出

```sh
python scripts/mongo_household.py export --output /private/household-export.json
```

正式 Docker 環境可在家庭服務資料夾執行下列命令；檔名請每次更換，完整匯出留在持久 `app-data` volume：

```sh
docker compose exec -T app python scripts/mongo_household.py export \
  --output /app/data/household-export-YYYYMMDD.json
docker compose cp app:/app/data/household-export-YYYYMMDD.json ./household-export-YYYYMMDD.json
chmod 600 ./household-export-YYYYMMDD.json
```

匯出檔使用 600 權限且拒絕覆蓋既有檔，保留完整批次、整數 ID、採買、位置、家具配置與 next-ID counters。這個 DTO 可用作未來本機／Firebase 搬遷輸入，但仍須驗證資料與契約；目前沒有自動同步。`/api/export` 是日常摘要，完整資料備份使用此 CLI；登入／提案／活動紀錄的本機 runtime 和 GLB 模型仍需另備份。

## 驗證範圍

`tests/test_mongo_inventory.py` 啟動隨機 loopback port、臨時 dbpath 的真實 mongod，不使用正式家庭資料、現有 localhost:27017 或任何 Firebase 服務。涵蓋雙 client 競爭消耗、地圖／庫存 version 衝突、採買失敗無部分寫入、批次與維護／搬移／刪除、匯入重跑與拒絕覆寫、完整匯出及刪除後不重用 ID、mongod 重啟持久性。缺少 mongod 會明確 skip，不冒充實測。

手動庫存和地圖不需要外部網路；手機仍須連到提供網頁的本機或家庭區網。Gemini 對話需外部 API，未設定金鑰時不影響手動操作。
