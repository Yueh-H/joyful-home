"""Fixed same-origin bridge; Studio owns vector geometry, this app owns sessions."""
import os
import re
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from . import contracts as c


def register_studio_proxy(app, authenticated, cookie_name, studio_url=None, transport=None, locations=None):
    upstream = (studio_url or os.getenv("MAP_STUDIO_URL", "http://127.0.0.1:8780")).rstrip("/")
    parsed = urlsplit(upstream)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("MAP_STUDIO_URL must be a bare trusted HTTP origin")

    assets = {
        "": ("/", "text/html", {"text/html"}),
        "app.js": ("/app.js", "application/javascript", {"application/javascript", "text/javascript"}),
        "contracts.js": ("/contracts.js", "application/javascript", {"application/javascript", "text/javascript"}),
        "styles.css": ("/styles.css", "text/css", {"text/css"}),
    }
    for name in ("model-viewer.js", "vendor/three.module.js", "vendor/three.core.js",
                 "vendor/OrbitControls.js", "vendor/GLTFLoader.js", "vendor/BufferGeometryUtils.js", "vendor/SkeletonUtils.js", "vendor/RoomEnvironment.js"):
        assets[name] = (f"/{name}", "application/javascript", {"application/javascript", "text/javascript"})
    assets["vendor/THREE-LICENSE.txt"] = ("/vendor/THREE-LICENSE.txt", "text/plain", {"text/plain"})

    async def forward(request, path, media_type="application/json", safe_types=None):
        token = request.cookies.get(cookie_name, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", token):
            raise HTTPException(401, "請先登入家庭管家。")
        headers = {"cookie": f"{cookie_name}={token}", "origin": upstream}
        if request.method not in {"GET", "HEAD"}:
            headers["content-type"] = "application/json"
        key = request.headers.get("idempotency-key", "")
        if key and len(key) <= 100 and all(32 <= ord(char) < 127 for char in key):
            headers["idempotency-key"] = key
        try:
            # No shared jar and no parent lock: Studio calls back into this app for auth/state.
            async with httpx.AsyncClient(base_url=upstream, transport=transport, timeout=20,
                                         follow_redirects=False, trust_env=False) as client:
                response = await client.request(request.method, path, headers=headers,
                    content=await request.body() if request.method not in {"GET", "HEAD"} else None)
        except httpx.RequestError:
            raise HTTPException(502, "地圖服務暫時無法連線，請稍後重試。") from None
        if response.is_redirect or response.status_code >= 500:
            raise HTTPException(502, "地圖服務暫時無法完成操作，請重新整理核對後重試。")
        received_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        allowed = safe_types or {"application/json"}
        if response.is_error:
            allowed, media_type = {"application/json"}, "application/json"
        if received_type not in allowed:
            raise HTTPException(502, "地圖服務回應格式不正確。")
        if media_type is None:
            media_type = received_type
        # Headers are constructed here, never relayed (including cookies, CSP, redirects).
        result = Response(response.content, status_code=response.status_code, media_type=media_type)
        if path == c.STUDIO_EXPORT and response.is_success:
            result.headers["Content-Disposition"] = 'attachment; filename="home-map.json"'
        return result

    @app.get(c.MAP_STUDIO + "{asset:path}", dependencies=[Depends(authenticated)])
    async def static_asset(asset: str, request: Request):
        if asset not in assets:
            raise HTTPException(404, "地圖資源不存在。")
        path, media_type, safe_types = assets[asset]
        return await forward(request, path, media_type, safe_types)

    async def api(request: Request, sid=Depends(authenticated)):
        if request.url.path == c.LOCATIONS and locations is not None:
            return {"locations": await run_in_threadpool(locations)}
        if request.url.path == c.STUDIO_REFERENCE:
            return await forward(request, c.STUDIO_REFERENCE, None, {"image/png", "image/jpeg", "image/webp"})
        return await forward(request, request.url.path)

    for path, method in ((c.STUDIO_LAYOUT, "GET"), (c.STUDIO_LAYOUT, "PUT"),
                         (c.STUDIO_REFERENCE, "GET"), (c.STUDIO_EXPORT, "GET"), (c.STUDIO_MODELS, "GET"), (c.LOCATIONS, "GET")):
        app.add_api_route(path, api, methods=[method])

    @app.get(c.STUDIO_MODEL, dependencies=[Depends(authenticated)])
    async def model_asset(object_id: str, request: Request):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", object_id):
            raise HTTPException(404, "找不到這件家具的模型。")
        return await forward(request, c.STUDIO_MODEL.format(object_id=object_id),
                             "model/gltf-binary", {"model/gltf-binary"})

    @app.post(c.STUDIO_CLEAR, dependencies=[Depends(authenticated)])
    async def clear_object(object_id: str, request: Request):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", object_id):
            raise HTTPException(422, "物件 ID 格式不正確。")
        return await forward(request, c.STUDIO_CLEAR.format(object_id=object_id))

    async def delete_item(item_id: str, request: Request):
        if not re.fullmatch(r"[1-9][0-9]{0,19}", item_id):
            raise HTTPException(422, "物品 ID 格式不正確。")
        return await forward(request, c.ITEMS + "/" + item_id)

    return delete_item
