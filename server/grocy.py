"""Fixed-route Grocy adapter. All inventory and maintenance metadata live in Grocy."""
from datetime import date, timedelta
import httpx
from .contracts import CATEGORIES, USERFIELDS

class GrocyError(Exception):
    pass

class Grocy:
    def __init__(self, url, key):
        self.client = httpx.Client(base_url=url.rstrip("/") + "/api/", headers={"GROCY-API-KEY": key}, timeout=20)
        self.ready = False

    def request(self, method, path, data=None):
        try:
            response = self.client.request(method, path, json=data)
            response.raise_for_status()
            return response.json() if response.content else None
        except (httpx.HTTPError, ValueError) as exc:
            # Do not echo upstream response bodies (may include configuration).
            raise GrocyError("Grocy 無法完成操作，請確認服務、連線與 API 設定。") from exc

    def objects(self, entity):
        return self.read_list(f"objects/{entity}")

    def read_list(self, path):
        result = self.request("GET", path)
        if result is None:  # Grocy represents empty entity/entry collections as HTTP 204.
            return []
        if not isinstance(result, list):
            raise GrocyError("Grocy 回傳的清單格式不正確，請確認服務版本與資料。")
        return result

    def ensure(self, entity, name, extra=None):
        for entry in self.objects(entity):
            if entry["name"] == name:
                return int(entry["id"])
        return int(self.request("POST", f"objects/{entity}", {"name": name, **(extra or {})})["created_object_id"])

    def locations(self):
        return [{"id": int(row["id"]), "name": row["name"]} for row in self.objects("locations")]

    def create_location(self, name):
        return {"id": self.ensure("locations", name), "name": name}

    def rename_location(self, location_id, name):
        self.request("PUT", f"objects/locations/{location_id}", {"name": name})
        return {"id": location_id, "name": name}

    def bootstrap(self):
        if self.ready:
            return
        existing = {(u["entity"], u["name"]) for u in self.objects("userfields")}
        for name, caption in USERFIELDS.items():
            if ("products", name) not in existing:
                self.request("POST", "objects/userfields", {"entity": "products", "name": name, "caption": caption, "type": "text"})
        for label in CATEGORIES.values():
            self.ensure("product_groups", label)
        self.ensure("quantity_units", "個", {"name_plural": "個"})
        self.ensure("locations", "未分類")
        self.ready = True

    @staticmethod
    def valid_date(value):
        if not value or str(value)[:4] in ("2999", "9999"):
            return None
        try:
            return date.fromisoformat(str(value)[:10]).isoformat()
        except ValueError:
            return None

    def state(self):
        # Read-only: schema initialization belongs to first explicit write/setup.
        products = self.objects("products")
        units = {int(x["id"]): x["name"] for x in self.objects("quantity_units")}
        locations = {int(x["id"]): x["name"] for x in self.objects("locations")}
        groups = {int(x["id"]): x["name"] for x in self.objects("product_groups")}
        entries = self.objects("stock")
        by_product = {}
        for row in entries:
            if float(row["amount"]) > 0:
                by_product.setdefault(int(row["product_id"]), []).append(row)
        items = []
        for product in products:
            pid = int(product["id"])
            fields = product.get("userfields") or {}
            rows = by_product.get(pid, [])
            nearest = min(rows, key=lambda r: r.get("best_before_date") or "2999-12-31") if rows else None
            dates = [d for r in rows if (d := self.valid_date(r.get("best_before_date")))]
            category = fields.get("category")
            if category not in CATEGORIES:
                group = groups.get(int(product.get("product_group_id") or 0))
                category = next((k for k, v in CATEGORIES.items() if v == group), "supplies")
            try:
                interval = int(fields.get("interval_days") or 0) or None
                if interval is not None and not 1 <= interval <= 36500:
                    interval = None
            except (ValueError, TypeError):
                interval = None
            last = self.valid_date(fields.get("last_maintained"))
            try:
                next_due = (date.fromisoformat(last) + timedelta(days=interval)).isoformat() if last and interval else None
            except OverflowError:
                next_due = None
            items.append({"id": pid, "name": product["name"], "category": category,
                "quantity": sum(float(r["amount"]) for r in rows), "unit": units.get(int(product["qu_id_stock"]), "個"),
                "location": locations.get(int(product["location_id"]), "未分類"), "location_id": int(product["location_id"]),
                "min_quantity": float(product.get("min_stock_amount") or 0), "best_before": min(dates) if dates else None,
                "opened": str(nearest.get("open", "0")) == "1" if nearest else False, "model": fields.get("model") or "",
                "description": product.get("description") or "", "interval_days": interval, "last_maintained": last, "next_due": next_due})
        index = {x["id"]: x for x in items}
        shopping = []
        for row in self.objects("shopping_list"):
            pid = int(row.get("product_id") or 0)
            if str(row.get("done", "0")) == "1":
                continue
            product = index.get(pid, {})
            shopping.append({"id": int(row["id"]), "item_id": pid, "name": product.get("name", row.get("note") or "未連結品項"),
                             "amount": float(row.get("amount") or 0), "unit": product.get("unit", "個")})
        return {"items": items, "shopping": shopping}

    def create(self, data):
        self.bootstrap()
        unit = self.ensure("quantity_units", data["unit"], {"name_plural": data["unit"]})
        location = self.ensure("locations", data["location"])
        group = self.ensure("product_groups", CATEGORIES[data["category"]])
        result = self.request("POST", "objects/products", {"name": data["name"], "description": data["description"],
            "location_id": location, "qu_id_purchase": unit, "qu_id_stock": unit,
            "product_group_id": group, "min_stock_amount": data["min_quantity"], "default_best_before_days": -1})
        pid = int(result["created_object_id"])
        self.metadata(pid, data)
        if data["quantity"]:
            self.add(pid, data["quantity"], data.get("best_before"), data.get("opened", False))
        return pid

    def metadata(self, pid, data):
        values = {k: str(data[k]) if data[k] is not None else "" for k in USERFIELDS if k in data}
        if values:
            self.request("PUT", f"userfields/products/{pid}", values)

    def patch(self, pid, data, current):
        self.bootstrap()
        payload = {}
        for key, field in (("name", "name"), ("description", "description"), ("min_quantity", "min_stock_amount")):
            if key in data:
                payload[field] = data[key]
        if "category" in data:
            payload["product_group_id"] = self.ensure("product_groups", CATEGORIES[data["category"]])
        if "location" in data:
            payload["location_id"] = self.ensure("locations", data["location"])
        if "unit" in data:
            unit = self.ensure("quantity_units", data["unit"], {"name_plural": data["unit"]})
            payload.update(qu_id_stock=unit, qu_id_purchase=unit)
        if payload:
            self.request("PUT", f"objects/products/{pid}", payload)
        self.metadata(pid, data)
        # Grocy deliberately rejects no-op inventory bookings. Full edit forms
        # include quantity even when only metadata changed.
        if "quantity" in data and data["quantity"] != current["quantity"]:
            self.request("POST", f"stock/products/{pid}/inventory", {"new_amount": data["quantity"], "best_before_date": data.get("best_before") or "2999-12-31"})
        # Scalar edits apply to the nearest-expiry batch. Other batches retain their dates.
        if any(k in data for k in ("best_before", "opened", "location")):
            rows = self.read_list(f"stock/products/{pid}/entries")
            rows.sort(key=lambda x: x.get("best_before_date") or "2999-12-31")
            if rows:
                row = rows[0]
                update = {k: row[k] for k in ("amount", "best_before_date", "purchased_date", "location_id", "price") if row.get(k) is not None}
                update["open"] = bool(int(row.get("open", 0)))
                if "best_before" in data:
                    update["best_before_date"] = data["best_before"] or "2999-12-31"
                if "opened" in data:
                    update["open"] = data["opened"]
                if "location" in data:
                    update["location_id"] = payload["location_id"]
                self.request("PUT", f"stock/entry/{row['id']}", update)

    def add(self, pid, amount, best_before=None, opened=False):
        bookings = self.request("POST", f"stock/products/{pid}/add", {"amount": amount, "best_before_date": best_before or "2999-12-31", "transaction_type": "purchase"})
        if opened:
            try:
                # stock_id binds opening to the batch just purchased, not an older batch.
                opening = self.request("POST", f"stock/products/{pid}/open", {"amount": amount, "stock_entry_id": bookings[0]["stock_id"]})
                bookings.extend(opening)
            except GrocyError:
                for transaction in {x["transaction_id"] for x in bookings}:
                    self.request("POST", f"stock/transactions/{transaction}/undo")
                raise GrocyError("開封更新失敗，這次入庫已撤銷；請重新整理後再試。") from None
        return bookings

    def action(self, action, today):
        pid, kind = action["item_id"], action["type"]
        if kind == "purchase":
            self.add(pid, action["amount"], action.get("best_before"), action.get("opened", False))
        elif kind == "consume":
            self.request("POST", f"stock/products/{pid}/consume", {"amount": action["amount"], "transaction_type": "consume"})
        elif kind == "maintain":
            self.bootstrap()
            self.metadata(pid, {"last_maintained": today})
        elif kind == "shopping_add":
            self.request("POST", "stock/shoppinglist/add-product", {"product_id": pid, "product_amount": action["amount"]})
        elif kind == "shopping_complete":
            bookings = self.add(pid, action["amount"], action.get("best_before"), action.get("opened", False))
            try:
                self.request("DELETE", f"objects/shopping_list/{action['shopping_id']}")
            except GrocyError:
                # HTTP response can be lost after a successful delete. Read back
                # before undoing; a missing row means the operation completed.
                if not any(int(x["id"]) == action["shopping_id"] for x in self.objects("shopping_list")):
                    return
                transactions = list(dict.fromkeys(x["transaction_id"] for x in reversed(bookings) if x.get("transaction_id")))
                if not transactions:
                    raise GrocyError("購物寫入未全部完成；請核對 Grocy 庫存，勿直接重試。") from None
                for transaction in transactions:
                    self.request("POST", f"stock/transactions/{transaction}/undo")
                raise GrocyError("購物清單更新失敗，這次入庫已撤銷；請重新整理後再試。") from None
