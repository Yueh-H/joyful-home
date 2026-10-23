"""Local Mongo household aggregate; version CAS preserves concurrent layout writes."""
import copy
from datetime import date, timedelta
from decimal import Decimal
import os
import re

from pydantic import ValidationError
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from pymongo.write_concern import WriteConcern
from . import contracts as c
# Pure helpers only; no Firestore client is constructed or contacted.
from .firestore_inventory import FirestoreInventoryError, batches, conflict, consume, number, numeric_id, set_batches


def empty_inventory():
    return {"locations": {}, "items": {}, "shopping": {}, "counters": {}}


class MongoInventoryError(FirestoreInventoryError):
    def __init__(self, detail="本機家庭資料庫暫時無法讀寫，請稍後重試。", status_code=503):
        super().__init__(detail, status_code)


class MongoInventory:
    backend = "mongodb"

    def __init__(self, client=None, uri=None, database=None, home_id=None, retries=5):
        self.home_id = home_id or os.getenv("HOME_ID", "home")
        dbname = database or os.getenv("MONGODB_DATABASE", "joyful_home")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.home_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", dbname):
            raise MongoInventoryError("本機家庭資料庫設定不正確。")
        self.client = client if client is not None else MongoClient(
            uri or os.getenv("MONGODB_URI", "mongodb://mongo:27017"),
            connect=False, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000,
            socketTimeoutMS=10000, retryWrites=False)
        self.collection = self.client[dbname].get_collection("households", write_concern=WriteConcern(w=1, j=True))
        self.retries = retries

    def document(self):
        document = self.collection.find_one({"_id": self.home_id})
        if document is None:
            raise MongoInventoryError("本機家庭資料尚未匯入，請先完成本機資料設定。")
        if document.get("schema_version") != 1 or isinstance(document.get("version"), bool) or not isinstance(document.get("version"), int) or document["version"] < 0:
            raise MongoInventoryError("本機家庭資料格式不正確。")
        if not isinstance(document.get("inventory"), dict) or not isinstance(document.get("layout"), dict):
            raise MongoInventoryError("本機家庭資料不完整。")
        return document

    def _run(self, operation, write=False):
        try:
            for _ in range(self.retries if write else 1):
                document = self.document()
                data = copy.deepcopy(document["inventory"])
                result = operation(data)
                if not write:
                    return result
                changed = self.collection.update_one(
                    {"_id": self.home_id, "schema_version": 1, "version": document["version"]},
                    {"$set": {"inventory": data}, "$inc": {"version": 1}})
                if changed.matched_count == 1:
                    return result
            conflict("家庭資料同時被其他操作更新，請重新整理後再試。")
        except FirestoreInventoryError:
            raise
        except ValidationError:
            raise MongoInventoryError("欄位格式不正確。", 422) from None
        except (PyMongoError, ValueError, KeyError, TypeError, OverflowError):
            # Driver errors can contain credentials/hosts; never echo them.
            raise MongoInventoryError() from None

    @staticmethod
    def _item(data, item_id):
        item = data["items"].get(str(numeric_id(item_id)))
        if item is None:
            raise MongoInventoryError("找不到這個物品，請重新整理。", 404)
        set_batches(item, batches(item))
        return item

    @staticmethod
    def _allocate(data, kind):
        previous = data["counters"].get(kind, 1)
        if isinstance(previous, bool) or not isinstance(previous, int) or previous < 1:
            raise MongoInventoryError("資料編號狀態不正確，請先核對資料。")
        value = numeric_id(previous)
        if str(value) in data[kind]:
            conflict("資料編號已存在，請先核對資料庫計數器。")
        data["counters"][kind] = numeric_id(value + 1)
        return value

    def _location(self, data, name):
        name = c.LocationName(name=name).name
        existing = next((row for row in data["locations"].values() if row["name"] == name), None)
        if existing:
            return existing
        row = {"id": self._allocate(data, "locations"), "name": name}
        data["locations"][str(row["id"])] = row
        return row

    def locations(self):
        return self._run(lambda data: sorted(data["locations"].values(), key=lambda row: row["id"]))

    @staticmethod
    def _state_from(data):
        items = list(data["items"].values())
        for item in items:
            set_batches(item, batches(item))
            item["location"] = data["locations"][str(item["location_id"])]["name"]
            last, interval = item.get("last_maintained"), item.get("interval_days")
            try:
                item["next_due"] = (date.fromisoformat(last) + timedelta(days=interval)).isoformat() if last and interval else None
            except OverflowError:
                item["next_due"] = None
            item.pop("batches", None)
        shopping = list(data["shopping"].values())
        for row in shopping:
            product = data["items"][str(row["item_id"])]
            row.update(name=product["name"], unit=product["unit"])
        return {"items": sorted(items, key=lambda row: row["id"]), "shopping": sorted(shopping, key=lambda row: row["id"])}

    def state(self):
        return self._run(self._state_from)

    def chat_state(self):
        """One fresh aggregate read: inventory and every map target share the same version."""
        from .chat_context import home_context
        try:
            document = self.document()
            state = self._state_from(copy.deepcopy(document["inventory"]))
            state["home_context"] = home_context(document)
            return state
        except MongoInventoryError:
            raise
        except (PyMongoError, ValueError, KeyError, TypeError):
            raise MongoInventoryError() from None

    def create_location(self, name):
        return self._run(lambda data: self._location(data, name), write=True)

    def rename_location(self, location_id, name):
        def write(data):
            key = str(numeric_id(location_id))
            validated = c.LocationName(name=name).name
            if key not in data["locations"]:
                raise MongoInventoryError("找不到這個位置。", 404)
            if any(row["name"] == validated and row["id"] != location_id for row in data["locations"].values()):
                conflict("已存在同名位置，請使用其他名稱。")
            data["locations"][key] = {"id": location_id, "name": validated}
            return data["locations"][key]
        return self._run(write, write=True)

    def create(self, fields):
        def write(data):
            item = c.ItemCreate.model_validate(fields).model_dump(mode="json")
            location = self._location(data, item["location"])
            item_id = self._allocate(data, "items")
            item.update(id=item_id, location_id=location["id"])
            set_batches(item, batches(item))
            data["items"][str(item_id)] = item
            return item_id
        return self._run(write, write=True)

    def patch(self, item_id, fields, current):
        def write(data):
            patch = c.ItemPatch.model_validate(fields).model_dump(mode="json", exclude_unset=True)
            if any(v is None and k not in {"best_before", "interval_days", "last_maintained"} for k, v in patch.items()):
                raise MongoInventoryError("這個欄位不可留空。", 422)
            item = self._item(data, item_id)
            rows = batches(item)
            if "quantity" in patch:
                if item["quantity"] != current["quantity"]:
                    conflict("庫存已由其他操作更新，請重新整理後再編輯。")
                delta = Decimal(str(patch["quantity"])) - Decimal(str(item["quantity"]))
                if delta < 0:
                    rows = consume(rows, float(-delta))
                elif delta > 0:
                    rows.append({"amount": float(delta), "best_before": patch.get("best_before"), "opened": patch.get("opened", False)})
            if rows:
                nearest = min(rows, key=lambda row: row.get("best_before") or "9999-12-31")
                for key in ("best_before", "opened"):
                    if key in patch:
                        nearest[key] = patch[key]
            item.update(patch)
            set_batches(item, rows)
            if "location" in patch:
                item["location_id"] = self._location(data, patch["location"])["id"]
        return self._run(write, write=True)

    def action(self, action, today):
        def write(data):
            operation = c.Action.model_validate(action).model_dump(mode="json", exclude_none=True)
            item_id, kind = operation["item_id"], operation["type"]
            item = self._item(data, item_id)
            rows = batches(item)
            if kind == "maintain":
                if "amount" in operation:
                    raise MongoInventoryError("維護紀錄不使用數量。", 422)
                item["last_maintained"] = date.fromisoformat(today).isoformat()
                return
            amount = number(operation.get("amount"), positive=True)
            if kind == "shopping_complete":
                shopping_id = numeric_id(operation.get("shopping_id"))
                row = data["shopping"].get(str(shopping_id))
                if row is None or row["item_id"] != item_id:
                    conflict("採買項目已變更，請重新整理。")
            if kind in {"purchase", "shopping_complete"}:
                rows.append({"amount": amount, "best_before": operation.get("best_before"), "opened": operation.get("opened", False)})
            elif kind == "consume":
                rows = consume(rows, amount)
            elif kind == "shopping_add":
                sid = self._allocate(data, "shopping")
                data["shopping"][str(sid)] = {"id": sid, "item_id": item_id, "amount": amount}
            set_batches(item, rows)
            if kind == "shopping_complete":
                del data["shopping"][str(shopping_id)]
        return self._run(write, write=True)

    def delete_item(self, item_id, confirm_name=None, expected_location_id=None, require_empty=False):
        def write(data):
            item = self._item(data, item_id)
            linked = [key for key, row in data["shopping"].items() if row["item_id"] == item_id]
            if require_empty:
                if item["quantity"] != 0 or linked:
                    conflict("仍有庫存或採買項目，請先確認清空後再移除物品。")
            elif confirm_name != item["name"] or numeric_id(expected_location_id) != item["location_id"]:
                conflict("物品名稱或儲位已變更，請重新整理並確認。")
            for key in linked:
                del data["shopping"][key]
            del data["items"][str(item_id)]
            return {"ok": True, "deleted_ids": [item_id]}
        return self._run(write, write=True)

    def request(self, method, path, data=None):
        if method == "GET" and path == "system/info":
            return self._run(lambda data: {"backend": self.backend, "connected": True})
        raise MongoInventoryError("此資料操作不受支援。", 422)


def validated_snapshot(state, locations):
    """Validate stable IDs/references and preserve supplied batch detail on import."""
    data = empty_inventory()
    for raw in locations:
        row = {"id": numeric_id(raw["id"]), "name": c.LocationName(name=raw["name"]).name}
        if str(row["id"]) in data["locations"] or any(row["name"] == old["name"] for old in data["locations"].values()):
            conflict("匯入位置資料重複。")
        data["locations"][str(row["id"])] = row
    for raw in state["items"]:
        iid, lid = numeric_id(raw["id"]), numeric_id(raw["location_id"])
        if str(iid) in data["items"] or str(lid) not in data["locations"]:
            conflict("匯入物品編號或位置連結不正確。")
        fields = {key: raw[key] for key in c.ItemCreate.model_fields if key in raw}
        fields["location"] = data["locations"][str(lid)]["name"]
        item = c.ItemCreate.model_validate(fields).model_dump(mode="json")
        item.update(id=iid, location_id=lid)
        set_batches(item, batches({**item, **({"batches": raw["batches"]} if "batches" in raw else {})}))
        data["items"][str(iid)] = item
    for raw in state["shopping"]:
        sid, iid = numeric_id(raw["id"]), numeric_id(raw["item_id"])
        if str(sid) in data["shopping"] or str(iid) not in data["items"]:
            conflict("匯入採買編號或物品連結不正確。")
        data["shopping"][str(sid)] = {"id": sid, "item_id": iid, "amount": number(raw["amount"], positive=True)}
    for kind in ("items", "locations", "shopping"):
        data["counters"][kind] = max((row["id"] for row in data[kind].values()), default=0) + 1
    return data
