# Firestore 正式資料庫

目前家庭系統使用獨立 Firebase 專案 `joyful-home-20261023`，Cloud Firestore Standard 的 `(default)` 資料庫，區域 `asia-east1`。已啟用資料庫防誤刪，未綁定付費帳單。

- [Firebase 資料庫管理](https://console.firebase.google.com/project/joyful-home-20261023/firestore/databases/-default-/data)
- [網頁入口](http://192.168.0.232:8765/#map)
- [獨立驗證](firestore-independent-verification.md)

## 唯一資料來源

正式資料位於 `households/home`：`items` 保存物品與效期批次、`locations` 保存位置、`shopping` 保存採買、`maps/layout` 保存向量房間／家具。數量變動、完成採買和確認刪除使用 transaction；配置保存以 revision 防止舊頁覆蓋新版本。Firestore 失敗會明確報錯，不回寫舊 Grocy 或 SQLite。

原始平面參考圖片仍保存在本機 `app-data` volume；正式可編輯地圖是 Firestore 的向量配置。登入 session、AI 提案、操作紀錄與 HTTP 去重紀錄屬於本機執行資料，繼續保存在 `app-data`。GitHub 不包含這些私人資料。

兩個服務使用同一個 Firestore 資料庫；Studio 的物品操作仍透過原家庭管家 API。瀏覽器用原家庭密碼登入，不持有服務憑證。`firestore.rules` 拒絕所有直接瀏覽器讀寫；後端 service account 僅在本專案具有 `roles/datastore.user`。

## 本機啟動

私人 `.env` 已設定 `INVENTORY_BACKEND=firestore`、`FIREBASE_PROJECT_ID`、`FIRESTORE_DATABASE_ID` 和 `FIRESTORE_HOUSEHOLD_ID`。Docker 使用外部 `joyful-home_firebase-credentials` volume，以唯讀方式提供 `/app/credentials/firebase.json`；檔案權限 600、擁有者為 app 的 UID 10001。主機的私人憑證副本在忽略版控的 `.runtime/`。

```sh
docker compose --profile app up -d --build app
```

接著在相鄰 `joyful-home-map` 執行：

```sh
uv run python scripts/start.py
```

手機仍使用原來的 8765 網址。資料已在雲端，但網頁後端目前仍由 Mac 執行；Mac 關機或 Docker 停止時，網址無法使用。這次未部署公開網站。

## 搬遷與回復

一次性搬遷腳本 `scripts/migrate_firestore.py` 先讀取舊服務，將完整 JSON snapshot 寫入權限 600 的 `.runtime/`。未加 `--apply` 時只備份，不寫雲端。請先停止其他編輯並確認來源服務正確：

```sh
uv run python scripts/migrate_firestore.py --project YOUR_PROJECT --credentials /private/path/credential.json
```

確認 snapshot 後才以 `--snapshot /private/path/snapshot.json --apply` 匯入。目的地必須是空的；只有已存在且雜湊相符的 migration marker 可恢復中斷的匯入。腳本驗證真實 Layout schema、穩定 ID 和雲端讀回結果，既有不同資料一律拒絕覆寫。

本次搬遷前的 Grocy、本機 SQLite volumes、`.env` 備份與私人 snapshot 均保留。不可在正式資料開始使用後直接切回舊庫，因為舊庫不再同步；需要回復時，先從 Firestore 匯出最新資料，再進行明確搬遷。資料庫防誤刪不等於定期備份或時間點回復。
