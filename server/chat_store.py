"""Journaled local chat turns, separate from the household inventory/layout aggregate."""
from datetime import datetime, timezone
import time
import re
import secrets
import base64
import json
from . import contracts as c
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.write_concern import WriteConcern


class ChatStoreError(Exception):
    def __init__(self, detail="本機對話紀錄暫時無法讀寫；請重新整理核對，勿視為已保存。", status_code=503):
        super().__init__(detail)
        self.status_code = status_code


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class MongoChat:
    # AI timeout is 45 seconds. An abandoned request never remains pending forever.
    PROCESSING_SECONDS = 120

    def __init__(self, database, home_id):
        self.indexed = False
        self.home_id = home_id
        self.collection = database.get_collection("chat_turns", write_concern=WriteConcern(w=1, j=True))
        self.conversations = database.get_collection("chat_conversations", write_concern=WriteConcern(w=1, j=True))
        self.sequences = database.get_collection("chat_sequences", write_concern=WriteConcern(w=1, j=True))

    def _run(self, operation):
        try:
            if not self.indexed:
                self.collection.create_index([("home_id", 1), ("order", -1)])
                self.conversations.create_index([("home_id", 1), ("updated_at", -1), ("conversation_id", -1)])
                self.indexed = True
            return operation()
        except ChatStoreError:
            raise
        except PyMongoError:
            # Driver messages may contain a private connection string.
            raise ChatStoreError() from None

    def conversation_query(self, conversation_id=None):
        conversation_id = conversation_id or c.CHAT_LEGACY
        query = {"home_id": self.home_id}
        query["conversation_id"] = {"$in": [None, c.CHAT_LEGACY]} if conversation_id == c.CHAT_LEGACY else conversation_id
        return query

    def require_conversation(self, conversation_id=None):
        conversation_id = conversation_id or c.CHAT_LEGACY
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", conversation_id):
            raise ChatStoreError("對話編號不正確。", 422)
        row = self.conversations.find_one({"_id": f"{self.home_id}:{conversation_id}", "home_id": self.home_id})
        if row is not None and "deleted_at" in row:
            raise ChatStoreError("這個對話已刪除，無法再開啟。", 410)
        if conversation_id != c.CHAT_LEGACY and row is None:
            raise ChatStoreError("找不到這個對話。", 404)
        return conversation_id

    def _legacy_metadata(self):
        """Materialize only the metadata override; never rewrite legacy turns."""
        last = self.collection.find_one(self.conversation_query(c.CHAT_LEGACY), {"created_at": 1}, sort=[("order", -1)])
        now = last["created_at"] if last else timestamp()
        self.conversations.update_one({"_id": f"{self.home_id}:{c.CHAT_LEGACY}", "home_id": self.home_id}, {"$setOnInsert": {
            "conversation_id": c.CHAT_LEGACY, "home_id": self.home_id, "title": "先前的對話",
            "created_at": now, "updated_at": now, "started": True}}, upsert=True)

    def rename_conversation(self, conversation_id, title):
        title = c.ConversationRename(title=title).title
        def write():
            self.require_conversation(conversation_id)
            if conversation_id == c.CHAT_LEGACY:
                self._legacy_metadata()
            row = self.conversations.find_one_and_update(
                {"_id": f"{self.home_id}:{conversation_id}", "home_id": self.home_id, "deleted_at": {"$exists": False}},
                {"$set": {"title": title, "custom_title": True, "updated_at": timestamp()}}, return_document=ReturnDocument.AFTER)
            if row is None:
                self.require_conversation(conversation_id)
                raise ChatStoreError()
            return {key: row[key] for key in ("conversation_id", "title", "created_at", "updated_at")}
        return self._run(write)

    def delete_conversation(self, conversation_id):
        def write():
            self.require_conversation(conversation_id)
            if conversation_id == c.CHAT_LEGACY:
                self._legacy_metadata()
            changed = self.conversations.update_one(
                {"_id": f"{self.home_id}:{conversation_id}", "home_id": self.home_id, "deleted_at": {"$exists": False}},
                {"$set": {"deleted_at": timestamp()}})
            if changed.matched_count != 1:
                self.require_conversation(conversation_id)
                raise ChatStoreError()
            return {"ok": True, "conversation_id": conversation_id}
        return self._run(write)

    def require_proposal(self, proposal_id):
        def read():
            row = self.collection.find_one({"home_id": self.home_id, "response.proposal.id": proposal_id}, {"conversation_id": 1})
            if row is None:
                raise ChatStoreError("找不到這份對話預覽，請重新提出要求。", 404)
            self.require_conversation(row.get("conversation_id"))
        return self._run(read)

    def create_conversation(self):
        def write():
            cid, now = secrets.token_urlsafe(24), timestamp()
            row = {"_id": f"{self.home_id}:{cid}", "home_id": self.home_id, "conversation_id": cid,
                   "title": "新增對話", "created_at": now, "updated_at": now, "started": False}
            self.conversations.insert_one(row)
            return {key: row[key] for key in ("conversation_id", "title", "created_at", "updated_at")}
        return self._run(write)

    def conversation_page(self, before=None, limit=30):
        cursor = None
        if before:
            try:
                cursor = json.loads(base64.urlsafe_b64decode(before + "=" * (-len(before) % 4)))
                if not isinstance(cursor, list) or len(cursor) != 2 or not all(isinstance(value, str) for value in cursor):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                raise ChatStoreError("歷史對話分頁編號不正確。", 422) from None
        def read():
            query = {"home_id": self.home_id, "deleted_at": {"$exists": False}}
            if cursor:
                query["$or"] = [{"updated_at": {"$lt": cursor[0]}}, {"updated_at": cursor[0], "conversation_id": {"$lt": cursor[1]}}]
            rows = list(self.conversations.find(query, {"_id": 0, "home_id": 0, "started": 0, "custom_title": 0}).sort([("updated_at", -1), ("conversation_id", -1)]).limit(limit + 1))
            legacy_query = self.conversation_query(c.CHAT_LEGACY)
            last = self.collection.find_one(legacy_query, {"created_at": 1}, sort=[("order", -1)])
            legacy_override = self.conversations.find_one({"_id": f"{self.home_id}:{c.CHAT_LEGACY}", "home_id": self.home_id})
            if last and legacy_override is None:
                row = {"conversation_id": c.CHAT_LEGACY, "title": "先前的對話", "created_at": last["created_at"], "updated_at": last["created_at"]}
                if cursor is None or (row["updated_at"], row["conversation_id"]) < tuple(cursor):
                    rows.append(row)
            rows.sort(key=lambda row: (row["updated_at"], row["conversation_id"]), reverse=True)
            more = len(rows) > limit
            rows = rows[:limit]
            next_cursor = base64.urlsafe_b64encode(json.dumps([rows[-1]["updated_at"], rows[-1]["conversation_id"]]).encode()).decode().rstrip("=") if more else None
            return rows, next_cursor
        return self._run(read)

    def reserve(self, request_id, message, images=None, conversation_id=None):
        conversation_id = self._run(lambda: self.require_conversation(conversation_id))
        document = {"conversation_id": conversation_id, "_id": f"{self.home_id}:{request_id}", "home_id": self.home_id,
                    "request_id": request_id, "message": message,
                    "created_at": timestamp(), "started": time.time(), "status": "processing",
                    "images": images or [], "image_fingerprints": [image["fingerprint"] for image in (images or [])]}
        def write():
            self.require_conversation(conversation_id)
            try:
                # Mongo allocates order across processes; ObjectId process bytes do not preserve it.
                sequence = self.sequences.find_one_and_update({"_id": self.home_id}, {"$inc": {"value": 1}},
                    upsert=True, return_document=ReturnDocument.AFTER)
                document["order"] = sequence["value"]
                self.collection.insert_one(document)
                target = {"_id": f"{self.home_id}:{conversation_id}"}
                if conversation_id != c.CHAT_LEGACY:
                    self.conversations.update_one({**target, "started": False, "custom_title": {"$ne": True}, "deleted_at": {"$exists": False}}, {"$set": {"started": True, "title": message.strip()[:80] or "圖片對話"}})
                self.conversations.update_one({**target, "deleted_at": {"$exists": False}}, {"$max": {"updated_at": document["created_at"]}})
                self.require_conversation(conversation_id)
                return document, True
            except DuplicateKeyError:
                existing = self.collection.find_one({"_id": document["_id"]}, {"images.data": 0})
                if existing is None or (existing.get("conversation_id") or c.CHAT_LEGACY) != conversation_id or existing["message"] != message or existing.get("image_fingerprints", []) != document["image_fingerprints"]:
                    raise ChatStoreError("這個訊息編號已用於其他內容，請重新送出。", 409)
                self.require_conversation(conversation_id)
                return existing, False
        return self._run(write)

    def finish(self, document, response=None, error=None, status_code=503):
        values = {"status": "failed" if error else "complete", "finished_at": timestamp()}
        if error:
            values.update(error=error, error_status=status_code)
        else:
            values["response"] = response
        def write():
            self.require_conversation(document.get("conversation_id"))
            result = self.collection.update_one({"_id": document["_id"], "status": "processing"}, {"$set": values})
            if result.matched_count != 1:
                raise ChatStoreError("對話保存狀態不明，請重新整理核對。")
            self.require_conversation(document.get("conversation_id"))
        self._run(write)

    def status(self, document):
        if document["status"] == "processing" and time.time() - document["started"] > self.PROCESSING_SECONDS:
            return "interrupted"
        return document["status"]

    def page(self, before=None, limit=50, conversation_id=None):
        if before and (not re.fullmatch(r"[0-9]{1,20}", before) or not 0 < int(before) < 2**63):
            raise ChatStoreError("對話分頁編號不正確。", 422)
        conversation_id = self._run(lambda: self.require_conversation(conversation_id))
        query = self.conversation_query(conversation_id)
        if before:
            query["order"] = {"$lt": int(before)}
        def read():
            self.require_conversation(conversation_id)
            rows = list(self.collection.find(query, {"images.data": 0}).sort("order", -1).limit(limit + 1))
            more = len(rows) > limit
            rows = rows[:limit]
            self.require_conversation(conversation_id)
            return list(reversed(rows)), str(rows[-1]["order"]).zfill(20) if more else None
        return self._run(read)

    def context(self, document):
        def read():
            self.require_conversation(document.get("conversation_id"))
            rows = list(self.collection.find({**self.conversation_query(document.get("conversation_id")), "status": "complete",
                                             "order": {"$lt": document["order"]}}, {"images": 0}).sort("order", -1).limit(8))
            turns = []
            for row in reversed(rows):
                turns.extend([{"role": "user", "content": row["message"]},
                              {"role": "assistant", "content": row["response"]["reply"][:6000]}])
            self.require_conversation(document.get("conversation_id"))
            return turns
        return self._run(read)

    def image(self, request_id, image_id):
        def read():
            row = self.collection.find_one({"_id": f"{self.home_id}:{request_id}"}, {"images": 1, "conversation_id": 1})
            if row:
                self.require_conversation(row.get("conversation_id"))
            image = next((image for image in row.get("images", []) if image["id"] == image_id), None) if row else None
            if image is None:
                raise ChatStoreError("找不到這張對話圖片。", 404)
            return image
        return self._run(read)

    def model_images(self, document, current):
        """At most four images: current first, then only the same eight completed exchanges."""
        self._run(lambda: self.require_conversation(document.get("conversation_id")))
        selected = [dict(image, source=f"本次訊息，圖片 {index + 1}") for index, image in enumerate(current)]
        if len(selected) >= 4:
            return selected
        def read():
            return list(self.collection.find({**self.conversation_query(document.get("conversation_id")), "status": "complete", "order": {"$lt": document["order"]}},
                {"images.data": 0}).sort("order", -1).limit(8))
        for row in self._run(read):
            for meta in row.get("images", []):
                image = self.image(row["request_id"], meta["id"])
                selected.append(dict(image, source=f"先前對話序號 {row['order']}，圖片 {int(meta['id']) + 1}"))
                if len(selected) >= 4:
                    return selected
        return selected
