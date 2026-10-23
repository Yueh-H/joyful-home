"""Firestore is the authoritative inventory store; cloud failures never fall back."""
from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import Decimal
import math
import os
import re

from pydantic import ValidationError

from . import contracts as c
from .grocy import GrocyError


class FirestoreInventoryError(GrocyError):
    def __init__(self, detail="家庭資料庫暫時無法連線，請稍後重新整理核對。", status_code=503):
        super().__init__(detail)
        self.status_code = status_code


def conflict(detail):
    raise FirestoreInventoryError(detail, 409)


def number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not (0 < value <= 1e9 if positive else 0 <= value <= 1e9):
        raise FirestoreInventoryError("數量必須是有效的非負數，操作量必須大於零。", 422)
    return float(value)


def numeric_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2**53 - 1:
        raise FirestoreInventoryError("資料 ID 格式不正確。", 422)
    return value


def batches(item):
    quantity = number(item.get("quantity", 0))
    rows = copy.deepcopy(item.get("batches"))
    if rows is None:
        rows = [{"amount": quantity, "best_before": item.get("best_before"), "opened": item.get("opened", False)}] if quantity else []
    if not isinstance(rows, list):
        raise FirestoreInventoryError()
    for row in rows:
        number(row["amount"], positive=True)
        if row.get("best_before") is not None:
            row["best_before"] = date.fromisoformat(row["best_before"]).isoformat()
        if not isinstance(row.get("opened", False), bool):
            raise FirestoreInventoryError()
        row.setdefault("opened", False)
    if abs(float(sum(Decimal(str(row["amount"])) for row in rows)) - quantity) > 1e-8:
        raise FirestoreInventoryError("庫存批次與總量不一致，請先核對資料。")
    return rows


def set_batches(item, rows):
    item["batches"] = rows
    item["quantity"] = number(float(sum(Decimal(str(row["amount"])) for row in rows)))
    nearest = min(rows, key=lambda row: row.get("best_before") or "9999-12-31") if rows else None
    item["best_before"] = nearest.get("best_before") if nearest else None
    item["opened"] = nearest.get("opened", False) if nearest else False


def consume(rows, amount):
    remaining = Decimal(str(amount))
    result = []
    for row in sorted(rows, key=lambda row: row.get("best_before") or "9999-12-31"):
        stock = Decimal(str(row["amount"]))
        taken = min(stock, remaining)
        remaining -= taken
        if stock > taken:
            result.append({**row, "amount": float(stock - taken)})
    if remaining:
        conflict("庫存不足，請重新整理後確認數量。")
    return result


class FirestoreInventory:
    backend = "firestore"

    def __init__(self, client=None, project_id=None, database_id=None, household_id=None, transactional=None):
        household = household_id or os.getenv("FIRESTORE_HOUSEHOLD_ID", "home")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", household):
            raise FirestoreInventoryError("家庭資料庫設定不正確。")
        try:
            from google.cloud import firestore
            if client is None:
                project = project_id or os.getenv("FIREBASE_PROJECT_ID")
                if not project:
                    raise FirestoreInventoryError("尚未設定家庭資料庫專案。")
                client = firestore.Client(project=project, database=database_id or os.getenv("FIRESTORE_DATABASE_ID", "(default)"))
            self.client = client
            self.transactional = transactional or firestore.transactional
            self.home = client.collection("households").document(household)
        except FirestoreInventoryError:
            raise
        except Exception:
            raise FirestoreInventoryError() from None

    def collection(self, name):
        return self.home.collection(name)

    def ref(self, kind, value):
        return self.collection(kind).document(str(numeric_id(value)))

    def _run(self, operation):
        try:
            return self.transactional(operation)(self.client.transaction())
        except FirestoreInventoryError:
            raise
        except ValidationError:
            raise FirestoreInventoryError("欄位格式不正確。", 422) from None
        except Exception:
            # SDK error messages can contain credential paths, project details or RPC data.
            raise FirestoreInventoryError() from None

    def _rows(self, tx, kind):
        return [snapshot.to_dict() | {"id": numeric_id(int(snapshot.id))}
                for snapshot in self.collection(kind).stream(transaction=tx)]

    def _item(self, tx, item_id):
        row = self.ref("items", item_id).get(transaction=tx)
        if not row.exists:
            raise FirestoreInventoryError("找不到這個物品，請重新整理。", 404)
        item = row.to_dict()
        item["id"] = item_id
        set_batches(item, batches(item))
        return item

    def _counters(self, tx):
        snapshot = self.collection("meta").document("counters").get(transaction=tx)
        return snapshot.to_dict() if snapshot.exists else {}

    def _allocate(self, tx, kind, counters):
        previous = counters.get(kind, 0)
        if isinstance(previous, bool) or not isinstance(previous, int) or previous < 0:
            raise FirestoreInventoryError("資料編號狀態不正確，請先核對資料。")
        value = numeric_id(previous + 1)
        if self.ref(kind, value).get(transaction=tx).exists:
            conflict("資料編號已存在，請先核對資料庫計數器。")
        counters[kind] = value
        return value

    def _location(self, tx, name, locations, counters):
        existing = next((row for row in locations if row["name"] == name), None)
        return existing or {"id": self._allocate(tx, "locations", counters), "name": name}

    def _save_counters(self, tx, counters):
        tx.set(self.collection("meta").document("counters"), counters)

    def locations(self):
        return self._run(lambda tx: sorted(self._rows(tx, "locations"), key=lambda row: row["id"]))

    def state(self):
        def read(tx):
            locations = {row["id"]: row["name"] for row in self._rows(tx, "locations")}
            items = self._rows(tx, "items")
            shopping = self._rows(tx, "shopping")
            for item in items:
                set_batches(item, batches(item))
                item["location"] = locations.get(item["location_id"], "未分類")
                last, interval = item.get("last_maintained"), item.get("interval_days")
                try:
                    item["next_due"] = (date.fromisoformat(last) + timedelta(days=interval)).isoformat() if last and interval else None
                except OverflowError:
                    item["next_due"] = None
                item.pop("batches", None)
            index = {item["id"]: item for item in items}
            for row in shopping:
                item = index.get(row["item_id"])
                if item is None:
                    raise FirestoreInventoryError("採買資料連結不一致，請先核對資料。")
                row.update(name=item["name"], unit=item["unit"])
            return {"items": sorted(items, key=lambda row: row["id"]), "shopping": sorted(shopping, key=lambda row: row["id"])}
        return self._run(read)

    def create_location(self, name):
        def write(tx):
            validated_name = c.LocationName(name=name).name
            locations, counters = self._rows(tx, "locations"), self._counters(tx)
            row = self._location(tx, validated_name, locations, counters)
            tx.set(self.ref("locations", row["id"]), row)
            self._save_counters(tx, counters)
            return row
        return self._run(write)

    def rename_location(self, location_id, name):
        def write(tx):
            validated_name = c.LocationName(name=name).name
            locations = self._rows(tx, "locations")
            if not any(row["id"] == location_id for row in locations):
                raise FirestoreInventoryError("找不到這個位置。", 404)
            if any(row["name"] == validated_name and row["id"] != location_id for row in locations):
                conflict("已存在同名位置，請使用其他名稱。")
            row = {"id": location_id, "name": validated_name}
            tx.set(self.ref("locations", location_id), row)
            return row
        return self._run(write)

    def create(self, data):
        def write(tx):
            item = c.ItemCreate.model_validate(data).model_dump(mode="json")
            locations, counters = self._rows(tx, "locations"), self._counters(tx)
            location = self._location(tx, item["location"], locations, counters)
            item_id = self._allocate(tx, "items", counters)
            item.update(id=item_id, location_id=location["id"])
            set_batches(item, batches(item))
            tx.set(self.ref("locations", location["id"]), location)
            tx.set(self.ref("items", item_id), item)
            self._save_counters(tx, counters)
            return item_id
        return self._run(write)

    def patch(self, item_id, data, current):
        def write(tx):
            patch = c.ItemPatch.model_validate(data).model_dump(mode="json", exclude_unset=True)
            if any(value is None and key not in {"best_before", "interval_days", "last_maintained"} for key, value in patch.items()):
                raise FirestoreInventoryError("這個欄位不可留空。", 422)
            item = self._item(tx, item_id)
            locations, counters = self._rows(tx, "locations"), self._counters(tx)
            location = self._location(tx, patch["location"], locations, counters) if "location" in patch else None
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
            if location:
                item["location_id"] = location["id"]
                tx.set(self.ref("locations", location["id"]), location)
                self._save_counters(tx, counters)
            tx.set(self.ref("items", item_id), item)
        return self._run(write)

    def action(self, action, today):
        def write(tx):
            operation = c.Action.model_validate(action).model_dump(mode="json", exclude_none=True)
            item_id, kind = operation["item_id"], operation["type"]
            item = self._item(tx, item_id)
            rows = batches(item)
            shopping = None
            counters = None
            if kind == "maintain":
                if "amount" in operation:
                    raise FirestoreInventoryError("維護紀錄不使用數量。", 422)
                item["last_maintained"] = date.fromisoformat(today).isoformat()
            else:
                amount = number(operation.get("amount"), positive=True)
                if kind == "shopping_complete":
                    shopping_id = numeric_id(operation.get("shopping_id"))
                    snapshot = self.ref("shopping", shopping_id).get(transaction=tx)
                    shopping = snapshot.to_dict() if snapshot.exists else None
                    if not shopping or shopping["item_id"] != item_id:
                        conflict("採買項目已變更，請重新整理。")
                if kind in {"purchase", "shopping_complete"}:
                    rows.append({"amount": amount, "best_before": operation.get("best_before"), "opened": operation.get("opened", False)})
                elif kind == "consume":
                    rows = consume(rows, amount)
                elif kind == "shopping_add":
                    counters = self._counters(tx)
                    shopping_id = self._allocate(tx, "shopping", counters)
                    shopping = {"id": shopping_id, "item_id": item_id, "amount": amount}
                set_batches(item, rows)
            # All transaction reads above must finish before the first write.
            tx.set(self.ref("items", item_id), item)
            if kind == "shopping_complete":
                tx.delete(self.ref("shopping", shopping_id))
            elif kind == "shopping_add":
                tx.set(self.ref("shopping", shopping_id), shopping)
                self._save_counters(tx, counters)
        return self._run(write)

    def delete_item(self, item_id, confirm_name=None, expected_location_id=None, require_empty=False):
        def write(tx):
            item = self._item(tx, item_id)
            linked = [row for row in self._rows(tx, "shopping") if row["item_id"] == item_id]
            if require_empty:
                if item["quantity"] != 0:
                    conflict("仍有庫存，請先確認並盤點為零，再移除物品。")
                if linked:
                    conflict("物品仍在購物清單，請先處理購物項目。")
            elif confirm_name != item["name"] or numeric_id(expected_location_id) != item["location_id"]:
                conflict("物品名稱或儲位已變更，請重新整理並確認。")
            if len(linked) > 450:
                conflict("關聯採買項目過多，請先整理清單後再刪除。")
            for row in linked:
                tx.delete(self.ref("shopping", row["id"]))
            tx.delete(self.ref("items", item_id))
            return {"ok": True, "deleted_ids": [item_id]}
        return self._run(write)

    def request(self, method, path, data=None):
        # Minimal compatibility for the existing health route; no arbitrary Grocy routes.
        if method == "GET" and path == "system/info":
            return self._run(lambda tx: {"backend": self.backend, "connected": self._counters(tx) is not None})
        raise FirestoreInventoryError("此資料操作不受支援。", 422)

    def import_snapshot(self, snapshot, locations):
        """One atomic initial import only; stable IDs and no overwriting a dataset."""
        def write(tx):
            existing = [self._rows(tx, kind) for kind in ("items", "locations", "shopping")]
            existing += [list(self.collection(kind).stream(transaction=tx)) for kind in ("maps", "meta")]
            if any(existing):
                conflict("目的資料庫已有家庭資料，拒絕覆寫。")
            if len(locations) + len(snapshot["items"]) + len(snapshot["shopping"]) > 450:
                raise FirestoreInventoryError("一次匯入資料過多，請使用已驗證的分批遷移流程。", 422)
            location_index = {}
            for raw in locations:
                row = {"id": numeric_id(raw["id"]), "name": c.LocationName(name=raw["name"]).name}
                if row["id"] in location_index or any(row["name"] == value["name"] for value in location_index.values()):
                    conflict("匯入位置資料重複。")
                location_index[row["id"]] = row
            item_index = {}
            for raw in snapshot["items"]:
                item_id = numeric_id(raw["id"])
                location_id = numeric_id(raw["location_id"])
                if item_id in item_index or location_id not in location_index:
                    conflict("匯入物品編號或位置連結不正確。")
                fields = {key: raw[key] for key in c.ItemCreate.model_fields if key in raw}
                fields["location"] = location_index[location_id]["name"]
                row = c.ItemCreate.model_validate(fields).model_dump(mode="json")
                row.update(id=item_id, location_id=location_id)
                set_batches(row, batches({**row, **({"batches": raw["batches"]} if "batches" in raw else {})}))
                item_index[item_id] = row
            shopping_index = {}
            for raw in snapshot["shopping"]:
                row = {"id": numeric_id(raw["id"]), "item_id": numeric_id(raw["item_id"]), "amount": number(raw["amount"], positive=True)}
                if row["id"] in shopping_index or row["item_id"] not in item_index:
                    conflict("匯入採買編號或物品連結不正確。")
                shopping_index[row["id"]] = row
            counters = {}
            for kind, index in (("items", item_index), ("locations", location_index), ("shopping", shopping_index)):
                counters[kind] = max(index, default=0)
                for item_id, row in index.items():
                    tx.set(self.ref(kind, item_id), row)
            self._save_counters(tx, counters)
            return {kind: len(index) for kind, index in (("items", item_index), ("locations", location_index), ("shopping", shopping_index))}
        return self._run(write)
