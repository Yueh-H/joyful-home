# 照片家具模型的整合

「家的地圖」中的照片模型由相鄰 Map Studio 提供。原服務只轉送已登入的固定模型 API 與明確列出的 JavaScript 資源，不保存第二份庫存或模型狀態。

- `GET /api/studio/models`：讀取 Studio 的私人模型 manifest。
- `GET /api/studio/models/{object_id}`：只允許既有 geometry ID 格式，轉送 `model/gltf-binary`；不接受任意 URL、檔案路徑或 query 轉送。
- `/map-studio/`：白名單新增 `model-viewer.js`、固定版本的 Three.js 核心與 OrbitControls／GLTFLoader 及必要 utilities、MIT license。其他檔案仍回 404。

模型 API 沿用原登入與 no-store，錯誤回應只接受 JSON；上游重新導向、錯誤 MIME 或未知資源不轉成 HTML。照片、GLB 與 Blender 原檔留在私人執行環境，不進此 repo。建模與分格功能契約由 Map Studio 的 `docs/contract.md` 說明。

僅地圖嵌入頁的 CSP `connect-src` 額外允許 `blob:`，供 GLTFLoader 讀取 GLB 內附貼圖；其他頁仍限同源。模型載入器拒絕外部素材網址，不需要放行遠端網站。

獨立邊界檢查見 `tests/test_model_proxy_independent.py`。
