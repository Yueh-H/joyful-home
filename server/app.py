"""Same-origin household API. Inventory is never persisted outside Grocy."""
from collections import defaultdict, deque
from datetime import datetime
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from . import contracts as c
from .ai import AIError, Gemini
from .grocy import Grocy, GrocyError
from .floorplan import register_floorplan

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
COOKIE = "joyful_session"


def today():
    return datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()


def fingerprint(state):
    snapshot = {k: state[k] for k in ("items", "shopping")}
    snapshot["today"] = today()
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def create_app(grocy=None, ai=None, db_path=None, password=None, secret=None):
    app = FastAPI(title="愉悅的家庭管家", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.grocy = grocy or Grocy(os.getenv("GROCY_URL", "http://127.0.0.1:9283"), os.getenv("GROCY_API_KEY", ""))
    app.state.ai = ai or Gemini()
    household_password = password if password is not None else os.getenv("HOME_PASSWORD", "")
    session_secret = secret if secret is not None else os.getenv("SESSION_SECRET", "")
    configured = bool(household_password and len(session_secret) >= 32)
    path = Path(db_path or os.getenv("RUNTIME_DB", str(ROOT / "runtime" / "app.sqlite3")))
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, session TEXT, snapshot TEXT, actions TEXT, expires REAL, status TEXT);
        CREATE TABLE IF NOT EXISTS activity (id TEXT PRIMARY KEY, at TEXT, message TEXT);
        CREATE TABLE IF NOT EXISTS requests (session TEXT, key TEXT, body TEXT, status TEXT, response TEXT, PRIMARY KEY(session,key));
        """)
    path.chmod(0o600)
    lock = threading.RLock()
    rates = defaultdict(deque)

    def database():
        return sqlite3.connect(path)

    def session_hash(token):
        return hmac.new(session_secret.encode(), token.encode(), hashlib.sha256).hexdigest()

    def current_session(request):
        token = request.cookies.get(COOKIE, "")
        if not configured or not token or len(token) > 200:
            return None
        sid = session_hash(token)
        with database() as db:
            row = db.execute("SELECT expires FROM sessions WHERE id=?", (sid,)).fetchone()
        return sid if row and row[0] > time.time() else None

    def authenticated(request: Request):
        sid = current_session(request)
        if not sid:
            raise HTTPException(401, "請先登入家庭管家。")
        return sid

    floorplan = register_floorplan(app, authenticated, database, path, lock)

    def rate_limit(key, limit, window):
        now = time.monotonic()
        with lock:
            values = rates[key]
            while values and values[0] < now - window:
                values.popleft()
            if len(values) >= limit:
                raise HTTPException(429, "操作太頻繁，請稍後再試。")
            values.append(now)

    @app.middleware("http")
    async def same_origin(request, call_next):
        if request.url.path.startswith(c.API):
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin")
                expected = str(request.base_url).rstrip("/")
                # Origin is mandatory for writes. curl/scripts send Origin explicitly.
                if origin != expected or request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
                    return JSONResponse({"detail": "此操作只允許從家庭管家同一來源送出。"}, status_code=403)
                if request.headers.get("content-type", "").split(";")[0] != "application/json":
                    return JSONResponse({"detail": "請使用 application/json。"}, status_code=415)
                length = request.headers.get("content-length", "0")
                body_limit = c.FLOORPLAN_BODY_LIMIT if request.url.path == c.FLOORPLAN and request.method == "PUT" else 100000
                if not length.isdigit() or int(length) > body_limit:
                    return JSONResponse({"detail": "請求內容過大。"}, status_code=413)
                # Enforce actual bytes too: chunked or forged Content-Length must not bypass limits.
                chunks, received = [], 0
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > body_limit:
                        return JSONResponse({"detail": "請求內容過大。"}, status_code=413)
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
        else:
            response = await call_next(request)
            # UI entry points must revalidate together after an application upgrade.
            response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "欄位格式不正確；數量須為有限非負數，操作量必須大於零。"}, status_code=422)

    @app.exception_handler(GrocyError)
    async def grocy_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(AIError)
    async def ai_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    def log(message):
        with database() as db:
            db.execute("INSERT INTO activity VALUES (?,?,?)", (secrets.token_urlsafe(12), datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds"), message))

    def get_state():
        state = app.state.grocy.state()
        with database() as db:
            state["activity"] = [{"id": r[0], "at": r[1], "message": r[2]} for r in db.execute("SELECT id,at,message FROM activity ORDER BY rowid DESC LIMIT 60")]
        state.update(ai={"enabled": app.state.ai.enabled, "provider": "Gemini"}, grocy={"connected": True}, today=today())
        return state

    def find_item(state, pid):
        item = next((x for x in state["items"] if x["id"] == pid), None)
        if item is None:
            raise HTTPException(404, "找不到這個物品，請重新整理。")
        return item

    def validate_patch(patch):
        nullable = {"best_before", "interval_days", "last_maintained"}
        if any(value is None and key not in nullable for key, value in patch.items()):
            raise HTTPException(422, "這個欄位不可留空。")

    def proposal_summary(actions, current):
        """Confirmation text describes the validated operations, never AI prose."""
        labels = {"name": "名稱", "category": "分類", "quantity": "數量", "unit": "單位", "location": "位置",
                  "min_quantity": "最低庫存", "best_before": "最近批次效期", "opened": "最近批次開封",
                  "model": "型號", "description": "說明", "interval_days": "維護間隔天數", "last_maintained": "上次維護日期"}
        verbs = {"purchase": "買入", "consume": "消耗", "shopping_add": "加入購物清單", "shopping_complete": "買入並完成購物"}
        summaries = []
        for operation in actions:
            kind = operation["type"]
            if kind == "create_item":
                data = operation["item"]
                text = f"新增「{data['name']}」，初始庫存 {data.get('quantity', 0):g} {data.get('unit', '個')}"
                text += f"，分類 {c.CATEGORIES[data['category']]}，位置 {data.get('location', '未分類')}"
                if data.get("interval_days"):
                    text += f"，維護間隔 {data['interval_days']} 天"
            else:
                item = find_item(current, operation["item_id"])
                if kind == "update_item":
                    changes = []
                    for key, value in operation["patch"].items():
                        if key == "category":
                            value = c.CATEGORIES[value]
                        elif isinstance(value, bool):
                            value = "是" if value else "否"
                        elif value is None:
                            value = "清除"
                        elif key in ("quantity", "min_quantity"):
                            value = f"{value:g} {operation['patch'].get('unit', item['unit'])}"
                        changes.append(f"{labels[key]}→{str(value)[:140]}")
                    text = f"編輯「{item['name']}」：" + "、".join(changes)
                elif kind == "maintain":
                    text = f"記錄「{item['name']}」於 {today()} 完成維護"
                else:
                    text = f"{verbs[kind]}「{item['name']}」{operation['amount']:g} {item['unit']}"
                    if kind in ("purchase", "shopping_complete"):
                        text += f"，效期 {operation.get('best_before') or '未填'}，{'已開封' if operation.get('opened') else '未開封'}"
            summaries.append(text)
        return "；\n".join(summaries)

    def validate_action(action, state):
        item = find_item(state, action["item_id"])
        kind, amount = action["type"], action.get("amount")
        if kind != "maintain" and amount is None:
            raise HTTPException(422, "請提供明確數量。")
        if kind == "maintain" and amount is not None:
            raise HTTPException(422, "維護紀錄不使用數量。")
        if kind == "consume" and amount > item["quantity"] + 1e-9:
            raise HTTPException(409, f"{item['name']} 庫存不足，目前為 {item['quantity']:g} {item['unit']}。")
        if kind == "shopping_complete":
            row = next((x for x in state["shopping"] if x["id"] == action.get("shopping_id") and x["item_id"] == item["id"]), None)
            if row is None:
                raise HTTPException(409, "購物項目已變更，請重新整理。")
        return item

    def execute(action):
        kind = action["type"]
        state = app.state.grocy.state()
        if kind == "create_item":
            pid = app.state.grocy.create(action["item"])
            message = f"已新增 {action['item']['name']}。"
        elif kind == "update_item":
            item = find_item(state, action["item_id"])
            validate_patch(action["patch"])
            app.state.grocy.patch(item["id"], action["patch"], item)
            message = f"已更新 {item['name']}。"
        else:
            item = validate_action(action, state)
            app.state.grocy.action(action, today())
            labels = {"purchase": "買入", "consume": "消耗", "maintain": "完成維護", "shopping_add": "加入購物清單", "shopping_complete": "買入並完成購物"}
            message = f"已{labels[kind]} {item['name']}" + (f" {action['amount']:g} {item['unit']}。" if kind != "maintain" else "。")
        log(message)
        return message

    @app.get(c.SESSION)
    def session(request: Request):
        return {"authenticated": bool(current_session(request)), "configured": configured}

    @app.post(c.LOGIN)
    def login(body: c.LoginInput, request: Request):
        rate_limit(("login", request.client.host if request.client else "unknown"), 10, 300)
        if not configured:
            raise HTTPException(503, "請先執行設定，產生家庭密碼與 SESSION_SECRET。")
        if not hmac.compare_digest(body.password.encode(), household_password.encode()):
            raise HTTPException(401, "家庭密碼不正確。")
        token = secrets.token_urlsafe(32)
        with database() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?)", (session_hash(token), time.time() + 7 * 86400))
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == "https", samesite="strict", max_age=7 * 86400, path="/")
        return response

    @app.post(c.LOGOUT)
    def logout(sid=Depends(authenticated)):
        with database() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (sid,))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
        return response

    @app.get(c.STATE)
    def state(sid=Depends(authenticated)):
        with lock:
            return get_state()

    @app.get(c.EXPORT)
    def export(sid=Depends(authenticated)):
        with lock:
            return JSONResponse({**get_state(), "floorplan": floorplan.read()}, headers={"Content-Disposition": f'attachment; filename="joyful-home-{today()}.json"'})

    @app.post(c.ITEMS)
    def create_item(body: c.ItemCreate, sid=Depends(authenticated)):
        with lock:
            pid = app.state.grocy.create(body.model_dump(mode="json"))
            log(f"已新增 {body.name}。")
            return {"ok": True, "item_id": pid}

    @app.patch(c.ITEMS + "/{item_id}")
    def patch_item(item_id: int, body: c.ItemPatch, sid=Depends(authenticated)):
        patch = body.model_dump(mode="json", exclude_unset=True)
        validate_patch(patch)
        with lock:
            item = find_item(app.state.grocy.state(), item_id)
            app.state.grocy.patch(item_id, patch, item)
            log(f"已更新 {item['name']}。")
            return {"ok": True}

    @app.delete(c.ITEMS + "/{item_id}")
    def delete_item(item_id: int, sid=Depends(authenticated)):
        with lock:
            current = app.state.grocy.state()
            item = find_item(current, item_id)
            if item["quantity"] != 0:
                raise HTTPException(409, "仍有庫存，請先確認並盤點為零，再移除物品。")
            if any(row["item_id"] == item_id for row in current["shopping"]):
                raise HTTPException(409, "物品仍在購物清單，請先處理購物項目。")
            app.state.grocy.request("DELETE", f"objects/products/{item_id}")
            log(f"已移除零庫存物品 {item['name']}。")
            return {"ok": True}

    @app.post(c.ACTIONS)
    def action(body: c.Action, request: Request, sid=Depends(authenticated)):
        with lock:
            key = request.headers.get("idempotency-key")
            payload = body.model_dump(mode="json", exclude_none=True)
            encoded = json.dumps(payload, sort_keys=True)
            if key:
                if not re.fullmatch(r"[a-zA-Z0-9_-]{16,100}", key):
                    raise HTTPException(422, "Idempotency-Key 格式不正確。")
                with database() as db:
                    row = db.execute("SELECT body,status,response FROM requests WHERE session=? AND key=?", (sid, key)).fetchone()
                    if row:
                        if row[0] != encoded or row[1] != "done":
                            raise HTTPException(409, "這次操作已送出；請重新整理核對結果。")
                        return json.loads(row[2])
                    # Validate before reserving a key; no writes have occurred yet.
                    validate_action(payload, app.state.grocy.state())
                    db.execute("INSERT INTO requests VALUES (?,?,?,?,?)", (sid, key, encoded, "processing", None))
            result = {"ok": True, "message": execute(payload)}
            if key:
                with database() as db:
                    db.execute("UPDATE requests SET status='done',response=? WHERE session=? AND key=?", (json.dumps(result), sid, key))
            return result

    @app.post(c.CHAT)
    def chat(body: c.ChatInput, sid=Depends(authenticated)):
        rate_limit(("chat", sid), 12, 60)
        with lock:
            current = get_state()
        result = app.state.ai.respond(body.message, [x.model_dump() for x in body.history], current, today())
        actions = []
        for planned in result.actions:
            operation = planned.model_dump(mode="json", exclude_none=True)
            if planned.patch is not None:
                operation["patch"] = planned.patch.model_dump(mode="json", exclude_unset=True)
            actions.append(operation)
        if not actions:
            return {"reply": result.reply, "proposal": None}
        simulated = copy.deepcopy(current)
        for action in actions:
            kind = action["type"]
            amount = action.get("amount")
            needs_quantity = kind in ("purchase", "consume", "shopping_add", "shopping_complete")
            if kind == "create_item":
                if "item" not in action:
                    raise HTTPException(422, "AI 新物品資料不完整，請重述或用手動新增。")
                amount = action["item"].get("quantity", 0)
                needs_quantity = amount > 0
            elif kind == "update_item":
                if not action.get("patch"):
                    raise HTTPException(422, "AI 編輯資料不完整。")
                validate_patch(action["patch"])
                item = find_item(simulated, action.get("item_id"))
                amount = action["patch"].get("quantity", 0)
                needs_quantity = "quantity" in action["patch"]
                item.update(action["patch"])
            else:
                # Revalidate fixed action schema independently of the model envelope.
                try:
                    c.Action.model_validate({k: v for k, v in action.items() if k in c.Action.model_fields})
                except ValidationError:
                    raise HTTPException(422, "AI 操作資料不完整，請重述或使用手動管理。") from None
                item = validate_action(action, simulated)
                if kind == "consume":
                    item["quantity"] -= action["amount"]
                elif kind in ("purchase", "shopping_complete"):
                    item["quantity"] += action["amount"]
                if kind == "shopping_complete":
                    simulated["shopping"] = [x for x in simulated["shopping"] if x["id"] != action["shopping_id"]]
            if needs_quantity:
                if not quantity_grounded(action.get("amount_source"), body.message, amount):
                    return {"reply": "請說明明確數量與單位，例如「消耗 2 顆雞蛋」。目前尚未修改資料。", "proposal": None}
            action.pop("amount_source", None)
        pid = secrets.token_urlsafe(24)
        with database() as db:
            db.execute("DELETE FROM proposals WHERE expires < ?", (time.time() - 86400,))
            db.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?)", (pid, sid, fingerprint(current), json.dumps(actions, ensure_ascii=False), time.time() + 600, "pending"))
        return {"reply": "已整理操作預覽，尚未寫入。請確認下方實際物品、數量與變更。", "proposal": {"id": pid, "summary": proposal_summary(actions, current), "actions": actions}}

    @app.post(c.PROPOSALS + "/{proposal_id}/apply")
    def apply_proposal(proposal_id: str, sid=Depends(authenticated)):
        with lock:
            with database() as db:
                row = db.execute("SELECT snapshot,actions,expires,status FROM proposals WHERE id=? AND session=?", (proposal_id, sid)).fetchone()
                if not row:
                    raise HTTPException(404, "找不到這份預覽。")
                if row[3] != "pending":
                    raise HTTPException(409, "這份預覽已處理，請重新整理確認資料，勿重複送出。")
                if row[2] < time.time() or fingerprint(app.state.grocy.state()) != row[0]:
                    raise HTTPException(409, "預覽已過期或庫存已變動，請重新提出要求。")
                claimed = db.execute("UPDATE proposals SET status='applying' WHERE id=? AND status='pending'", (proposal_id,))
                if claimed.rowcount != 1:
                    raise HTTPException(409, "這份預覽已處理，請重新整理。")
            completed = 0
            try:
                for operation in json.loads(row[1]):
                    execute(operation)
                    completed += 1
            except Exception:
                with database() as db:
                    db.execute("UPDATE proposals SET status='failed' WHERE id=?", (proposal_id,))
                raise HTTPException(502, f"操作未全部完成（已完成 {completed} 步）；請重新整理核對 Grocy，這份預覽已鎖定，避免重複寫入。") from None
            with database() as db:
                db.execute("UPDATE proposals SET status='applied' WHERE id=?", (proposal_id,))
            return {"ok": True, "message": f"已確認並完成 {completed} 項變更。"}

    @app.get("/health")
    def health():
        try:
            app.state.grocy.request("GET", "system/info")
            return {"ok": True, "grocy": True}
        except GrocyError:
            return JSONResponse({"ok": False, "grocy": False}, status_code=503)

    if (ROOT / "web").exists():
        app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
    return app


def quantity_grounded(source, message, amount):
    """Require a verifiable exact quantity token; model cannot fill unknown counts."""
    if not source or source not in message:
        return False
    if source[0].isdigit() and not re.search(r"(?<![\d.])" + re.escape(source) + r"(?![\d.])", message):
        return False
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?|[零一二兩三四五六七八九十百半]+)\s*(?:[\u4e00-\u9fffa-zA-Z]+)?\s*", source)
    if not match:
        return False
    token = match.group(1)
    digits = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    try:
        value = float(token)
    except ValueError:
        if token == "半":
            value = 0.5
        else:
            value, unit = 0, 0
            for char in token:
                if char in digits:
                    unit = digits[char]
                elif char in ("十", "百"):
                    value += (unit or 1) * (10 if char == "十" else 100)
                    unit = 0
                else:
                    return False
            value += unit
    return abs(value - amount) < 1e-9


app = create_app()
