"""Journaled local chat turns, separate from the household inventory/layout aggregate."""
from datetime import datetime, timezone
import time
import re
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
        self.sequences = database.get_collection("chat_sequences", write_concern=WriteConcern(w=1, j=True))

    def _run(self, operation):
        try:
            if not self.indexed:
                self.collection.create_index([("home_id", 1), ("order", -1)])
                self.indexed = True
            return operation()
        except ChatStoreError:
            raise
        except PyMongoError:
            # Driver messages may contain a private connection string.
            raise ChatStoreError() from None

    def reserve(self, request_id, message, images=None):
        document = {"_id": f"{self.home_id}:{request_id}", "home_id": self.home_id,
                    "request_id": request_id, "message": message,
                    "created_at": timestamp(), "started": time.time(), "status": "processing",
                    "images": images or [], "image_fingerprints": [image["fingerprint"] for image in (images or [])]}
        def write():
            try:
                # Mongo allocates order across processes; ObjectId process bytes do not preserve it.
                sequence = self.sequences.find_one_and_update({"_id": self.home_id}, {"$inc": {"value": 1}},
                    upsert=True, return_document=ReturnDocument.AFTER)
                document["order"] = sequence["value"]
                self.collection.insert_one(document)
                return document, True
            except DuplicateKeyError:
                existing = self.collection.find_one({"_id": document["_id"]}, {"images.data": 0})
                if existing is None or existing["message"] != message or existing.get("image_fingerprints", []) != document["image_fingerprints"]:
                    raise ChatStoreError("這個訊息編號已用於其他內容，請重新送出。", 409)
                return existing, False
        return self._run(write)

    def finish(self, document, response=None, error=None, status_code=503):
        values = {"status": "failed" if error else "complete", "finished_at": timestamp()}
        if error:
            values.update(error=error, error_status=status_code)
        else:
            values["response"] = response
        def write():
            result = self.collection.update_one({"_id": document["_id"], "status": "processing"}, {"$set": values})
            if result.matched_count != 1:
                raise ChatStoreError("對話保存狀態不明，請重新整理核對。")
        self._run(write)

    def status(self, document):
        if document["status"] == "processing" and time.time() - document["started"] > self.PROCESSING_SECONDS:
            return "interrupted"
        return document["status"]

    def page(self, before=None, limit=50):
        if before and (not re.fullmatch(r"[0-9]{1,20}", before) or not 0 < int(before) < 2**63):
            raise ChatStoreError("對話分頁編號不正確。", 422)
        query = {"home_id": self.home_id}
        if before:
            query["order"] = {"$lt": int(before)}
        def read():
            rows = list(self.collection.find(query, {"images.data": 0}).sort("order", -1).limit(limit + 1))
            more = len(rows) > limit
            rows = rows[:limit]
            return list(reversed(rows)), str(rows[-1]["order"]).zfill(20) if more else None
        return self._run(read)

    def context(self, document):
        def read():
            rows = list(self.collection.find({"home_id": self.home_id, "status": "complete",
                                             "order": {"$lt": document["order"]}}, {"images": 0}).sort("order", -1).limit(8))
            turns = []
            for row in reversed(rows):
                turns.extend([{"role": "user", "content": row["message"]},
                              {"role": "assistant", "content": row["response"]["reply"][:6000]}])
            return turns
        return self._run(read)

    def image(self, request_id, image_id):
        def read():
            row = self.collection.find_one({"_id": f"{self.home_id}:{request_id}"}, {"images": 1})
            image = next((image for image in row.get("images", []) if image["id"] == image_id), None) if row else None
            if image is None:
                raise ChatStoreError("找不到這張對話圖片。", 404)
            return image
        return self._run(read)

    def model_images(self, document, current):
        """At most four images: current first, then only the same eight completed exchanges."""
        selected = [dict(image, source=f"本次訊息，圖片 {index + 1}") for index, image in enumerate(current)]
        if len(selected) >= 4:
            return selected
        def read():
            return list(self.collection.find({"home_id": self.home_id, "status": "complete", "order": {"$lt": document["order"]}},
                {"images.data": 0}).sort("order", -1).limit(8))
        for row in self._run(read):
            for meta in row.get("images", []):
                image = self.image(row["request_id"], meta["id"])
                selected.append(dict(image, source=f"先前對話序號 {row['order']}，圖片 {int(meta['id']) + 1}"))
                if len(selected) >= 4:
                    return selected
        return selected
