"""Offline serializable Firestore tests: no credentials, network or family records."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from server import app as app_module
from server.firestore_inventory import FirestoreInventory, FirestoreInventoryError


class RetryTransaction(Exception):
    pass


class Snapshot:
    def __init__(self, path, value):
        self.id = path.rsplit("/", 1)[-1]
        self.exists = value is not None
        self.value = copy.deepcopy(value)

    def to_dict(self):
        return copy.deepcopy(self.value)


class Document:
    def __init__(self, client, path):
        self.client, self.path = client, path

    def collection(self, name):
        return Collection(self.client, self.path + "/" + name)

    def get(self, transaction=None):
        transaction.read()
        return Snapshot(self.path, transaction.snapshot.get(self.path))


class Collection:
    def __init__(self, client, path):
        self.client, self.path = client, path

    def document(self, name):
        return Document(self.client, self.path + "/" + name)

    def stream(self, transaction=None):
        transaction.read()
        prefix = self.path + "/"
        return [Snapshot(path, value) for path, value in transaction.snapshot.items()
                if path.startswith(prefix) and "/" not in path[len(prefix):]]


class Transaction:
    def __init__(self, client):
        self.client = client

    def begin(self):
        with self.client.lock:
            self.version = self.client.version
            self.snapshot = copy.deepcopy(self.client.data)
        self.pending = {}
        self.writing = False

    def read(self):
        assert not self.writing, "Firestore forbids transaction reads after writes"
        if self.client.failure == "read":
            raise RuntimeError("synthetic SDK credential /private/test-key.json")

    def set(self, ref, value):
        self.writing = True
        if self.client.failure == "write":
            raise RuntimeError("synthetic private RPC write data")
        self.pending[ref.path] = copy.deepcopy(value)

    def delete(self, ref):
        self.writing = True
        self.pending[ref.path] = None
        if self.client.failure == "delete":
            raise RuntimeError("synthetic private delete response")

    def commit(self):
        with self.client.lock:
            if self.client.version != self.version or self.client.force_retry:
                self.client.force_retry = False
                self.client.retries += 1
                raise RetryTransaction()
            if self.client.failure == "commit":
                raise RuntimeError("synthetic private commit details")
            for path, value in self.pending.items():
                if value is None:
                    self.client.data.pop(path, None)
                else:
                    self.client.data[path] = value
            if self.pending:
                self.client.version += 1


class FakeClient:
    def __init__(self):
        self.data = {}
        self.version = 0
        self.retries = 0
        self.failure = None
        self.force_retry = False
        self.lock = threading.Lock()

    def collection(self, name):
        return Collection(self, name)

    def transaction(self):
        return Transaction(self)


def fake_transactional(operation):
    def run(tx):
        for _ in range(100):
            tx.begin()
            try:
                result = operation(tx)
                tx.commit()
                return result
            except RetryTransaction:
                continue
        raise RuntimeError("fake transaction retry budget exhausted")
    return run


def inventory(client=None, household="synthetic-qa"):
    return FirestoreInventory(client=client if client is not None else FakeClient(), household_id=household,
                              transactional=fake_transactional)


def sample():
    return {"items": [{"id": 11, "name": "測試牛奶", "category": "food", "quantity": 3.5, "unit": "瓶",
            "location": "冰箱", "location_id": 7, "best_before": "2026-11-01", "opened": False,
            "interval_days": 7, "last_maintained": "2026-10-23",
            "batches": [{"amount": 1.5, "best_before": "2026-11-01", "opened": False},
                        {"amount": 2.0, "best_before": "2026-12-01", "opened": True}]}],
            "shopping": [{"id": 22, "item_id": 11, "amount": 2}]}


LOCATIONS = [{"id": 7, "name": "冰箱"}, {"id": 8, "name": "抽屜"}]


@pytest.fixture
def rig():
    adapter = inventory()
    assert adapter.import_snapshot(sample(), LOCATIONS) == {"items": 1, "locations": 2, "shopping": 1}
    return adapter, adapter.client


def item(adapter):
    return adapter.state()["items"][0]


def operation(adapter, kind, amount=None, **extra):
    adapter.action({"type": kind, "item_id": 11, **({"amount": amount} if amount is not None else {}), **extra}, "2026-10-23")


def test_import_preserves_numeric_ids_counters_and_public_api(rig):
    adapter, client = rig
    state = adapter.state()
    assert item(adapter)["id"] == 11 and item(adapter)["quantity"] == 3.5
    assert item(adapter)["location_id"] == 7 and item(adapter)["location"] == "冰箱"
    assert item(adapter)["best_before"] == "2026-11-01" and item(adapter)["opened"] is False
    assert item(adapter)["next_due"] == "2026-10-30" and "batches" not in item(adapter)
    assert state["shopping"] == [{"id": 22, "item_id": 11, "amount": 2, "name": "測試牛奶", "unit": "瓶"}]
    assert client.data["households/synthetic-qa/meta/counters"] == {"items": 11, "locations": 8, "shopping": 22}
    assert adapter.create({"name": "測試空盒", "category": "supplies", "location": "抽屜"}) == 12
    assert adapter.create_location("測試新位置") == {"id": 9, "name": "測試新位置"}
    assert adapter.create_location("測試新位置")["id"] == 9


def test_import_refuses_nonempty_destination_and_keeps_existing_data(rig):
    adapter, client = rig
    before = copy.deepcopy(client.data)
    with pytest.raises(FirestoreInventoryError) as caught:
        adapter.import_snapshot(sample(), LOCATIONS)
    assert caught.value.status_code == 409 and client.data == before


@pytest.mark.parametrize("kind", ["maps", "meta"])
def test_import_refuses_existing_map_or_metadata(kind):
    adapter = inventory()
    adapter.client.data[f"households/synthetic-qa/{kind}/existing"] = {"synthetic": True}
    with pytest.raises(FirestoreInventoryError, match="拒絕覆寫"):
        adapter.import_snapshot({"items": [], "shopping": []}, LOCATIONS)
    assert len(adapter.client.data) == 1


def test_invalid_import_is_atomic(rig):
    adapter = inventory()
    snapshot = sample()
    snapshot["shopping"][0]["item_id"] = 999
    with pytest.raises(FirestoreInventoryError):
        adapter.import_snapshot(snapshot, LOCATIONS)
    assert adapter.client.data == {}


def test_purchase_preserves_older_batches_and_consume_is_fefo(rig):
    adapter, client = rig
    operation(adapter, "purchase", 1, best_before="2027-01-01", opened=True)
    assert item(adapter)["quantity"] == 4.5 and item(adapter)["opened"] is False
    operation(adapter, "consume", 2)
    assert item(adapter)["quantity"] == 2.5 and item(adapter)["best_before"] == "2026-12-01"
    assert item(adapter)["opened"] is True
    rows = client.data["households/synthetic-qa/items/11"]["batches"]
    assert rows == [{"amount": 1.5, "best_before": "2026-12-01", "opened": True},
                    {"amount": 1.0, "best_before": "2027-01-01", "opened": True}]


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf"), 1e10])
def test_invalid_quantities_never_write(rig, amount):
    adapter, client = rig
    before = copy.deepcopy(client.data)
    with pytest.raises(FirestoreInventoryError):
        operation(adapter, "purchase", amount)
    assert client.data == before


def test_overconsumption_rejected_without_partial_batch_change(rig):
    adapter, client = rig
    before = copy.deepcopy(client.data)
    with pytest.raises(FirestoreInventoryError) as caught:
        operation(adapter, "consume", 4)
    assert caught.value.status_code == 409 and client.data == before


def test_complete_shopping_is_one_transaction_and_replay_conflicts(rig):
    adapter, client = rig
    operation(adapter, "shopping_complete", 2, shopping_id=22, best_before="2027-01-01", opened=True)
    assert item(adapter)["quantity"] == 5.5 and adapter.state()["shopping"] == []
    before = copy.deepcopy(client.data)
    with pytest.raises(FirestoreInventoryError):
        operation(adapter, "shopping_complete", 2, shopping_id=22)
    assert client.data == before


@pytest.mark.parametrize("failure", ["write", "delete", "commit"])
def test_shopping_failure_rolls_back_stock_and_list_together(rig, failure):
    adapter, client = rig
    before = copy.deepcopy(client.data)
    client.failure = failure
    with pytest.raises(FirestoreInventoryError) as caught:
        operation(adapter, "shopping_complete", 2, shopping_id=22)
    assert caught.value.status_code == 503 and "private" not in str(caught.value)
    assert client.data == before


def test_shopping_allocation_and_live_name_unit(rig):
    adapter, _ = rig
    operation(adapter, "shopping_add", 3)
    assert [row["id"] for row in adapter.state()["shopping"]] == [22, 23]
    adapter.patch(11, {"name": "新的名字", "unit": "包"}, item(adapter))
    assert all(row["name"] == "新的名字" and row["unit"] == "包" for row in adapter.state()["shopping"])


def test_patch_quantity_batches_metadata_and_location(rig):
    adapter, client = rig
    adapter.patch(11, {"quantity": 2.5, "best_before": "2026-11-05", "opened": True, "location": "新儲位"}, item(adapter))
    current = item(adapter)
    assert current["quantity"] == 2.5 and current["location_id"] == 9 and current["best_before"] == "2026-11-05"
    rows = client.data["households/synthetic-qa/items/11"]["batches"]
    assert rows[1] == {"amount": 2.0, "best_before": "2026-12-01", "opened": True}
    adapter.patch(11, {"quantity": 0}, current)
    assert item(adapter)["quantity"] == 0 and item(adapter)["best_before"] is None


def test_stale_quantity_patch_cannot_overwrite_a_concurrent_purchase(rig):
    adapter, client = rig
    stale = item(adapter)
    operation(adapter, "purchase", 1)
    before = copy.deepcopy(client.data)
    with pytest.raises(FirestoreInventoryError) as caught:
        adapter.patch(11, {"quantity": stale["quantity"], "name": "過期編輯"}, stale)
    assert caught.value.status_code == 409 and client.data == before


def test_location_rename_updates_item_view_without_rewriting_item(rig):
    adapter, client = rig
    before = copy.deepcopy(client.data["households/synthetic-qa/items/11"])
    adapter.rename_location(7, "新的冰箱")
    assert item(adapter)["location"] == "新的冰箱"
    assert client.data["households/synthetic-qa/items/11"] == before
    with pytest.raises(FirestoreInventoryError):
        adapter.rename_location(7, "抽屜")


def test_maintain_updates_due_date(rig):
    adapter, _ = rig
    adapter.action({"type": "maintain", "item_id": 11}, "2026-10-24")
    assert item(adapter)["last_maintained"] == "2026-10-24" and item(adapter)["next_due"] == "2026-10-31"


def test_guarded_delete_cleans_only_linked_shopping_atomically(rig):
    adapter, _ = rig
    other = adapter.create({"name": "其他物品", "category": "supplies", "location": "抽屜"})
    adapter.action({"type": "shopping_add", "item_id": other, "amount": 1}, "2026-10-23")
    assert adapter.delete_item(11, confirm_name="測試牛奶", expected_location_id=7) == {"ok": True, "deleted_ids": [11]}
    assert [row["id"] for row in adapter.state()["items"]] == [other]
    assert all(row["item_id"] == other for row in adapter.state()["shopping"])


@pytest.mark.parametrize("kwargs", [{"confirm_name": "錯名字", "expected_location_id": 7},
    {"confirm_name": "測試牛奶", "expected_location_id": 8}, {"require_empty": True}])
def test_delete_confirmation_and_legacy_guards(rig, kwargs):
    adapter, client = rig
    before = copy.deepcopy(client.data)
    with pytest.raises(FirestoreInventoryError) as caught:
        adapter.delete_item(11, **kwargs)
    assert caught.value.status_code == 409 and client.data == before


def test_legacy_empty_delete_requires_zero_and_no_shopping(rig):
    adapter, client = rig
    adapter.patch(11, {"quantity": 0}, item(adapter))
    with pytest.raises(FirestoreInventoryError):
        adapter.delete_item(11, require_empty=True)
    client.data.pop("households/synthetic-qa/shopping/22")
    assert adapter.delete_item(11, require_empty=True)["deleted_ids"] == [11]


def test_multiple_adapters_share_transactions_no_lost_purchase_or_ids(rig):
    adapter, client = rig
    other = inventory(client)
    client.force_retry = True
    def add(index):
        operation(adapter if index % 2 else other, "purchase", 0.25)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(32)))
    assert item(adapter)["quantity"] == 11.5 and client.retries >= 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda index: (adapter if index % 2 else other).create(
            {"name": f"測試物品{index}", "category": "supplies", "location": "冰箱"}), range(20)))
    assert sorted(ids) == list(range(12, 32)) and len(adapter.state()["items"]) == 21


def test_concurrent_consumers_cannot_take_negative_stock(rig):
    adapter, client = rig
    def take(_):
        try:
            operation(inventory(client), "consume", 1)
            return True
        except FirestoreInventoryError as error:
            assert error.status_code == 409
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(take, range(12)))
    assert sum(results) == 3 and item(adapter)["quantity"] == 0.5


def test_concurrent_shopping_completion_happens_once(rig):
    adapter, client = rig
    def complete(_):
        try:
            operation(inventory(client), "shopping_complete", 2, shopping_id=22)
            return True
        except FirestoreInventoryError as error:
            assert error.status_code == 409
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(complete, range(2)))
    assert sum(results) == 1 and item(adapter)["quantity"] == 5.5


class NoAI:
    enabled = False


def api(adapter, tmp_path):
    def no_studio(request):
        raise AssertionError("Firestore inventory must not proxy back to Studio")
    app = app_module.create_app(adapter, NoAI(), tmp_path / "auth.db", "test-household", "x" * 40,
                                studio_transport=httpx.MockTransport(no_studio))
    client = TestClient(app, headers={"origin": "http://testserver"})
    assert client.post("/api/login", json={"password": "test-household"}).status_code == 200
    return client


def test_parent_firestore_locations_and_explicit_delete_do_not_proxy(rig, tmp_path):
    adapter, _ = rig
    client = api(adapter, tmp_path)
    assert client.get("/api/locations").json() == {"locations": LOCATIONS}
    assert client.get("/api/state").json()["inventory"] == {"backend": "firestore"}
    assert client.request("DELETE", "/api/items/11", json={}).status_code == 409
    assert client.request("DELETE", "/api/items/11", json={"confirm_name": "wrong", "expected_location_id": 7}).status_code == 409
    assert client.request("DELETE", "/api/items/11", json={"confirm_name": "測試牛奶", "expected_location_id": 7}).json() == {"ok": True, "deleted_ids": [11]}
    assert adapter.state() == {"items": [], "shopping": []}


@pytest.mark.parametrize("body", [{"confirm_name": "測試牛奶"}, {"confirm_name": "測試牛奶", "expected_location_id": True},
    {"confirm_name": "測試牛奶", "expected_location_id": 7, "extra": "invalid"}, None, []])
def test_parent_explicit_delete_strict_confirmation(rig, tmp_path, body):
    adapter, client_data = rig
    client = api(adapter, tmp_path)
    before = copy.deepcopy(client_data.data)
    result = client.request("DELETE", "/api/items/11", content=json.dumps(body), headers={"content-type": "application/json"})
    assert result.status_code == 422 and client_data.data == before


def test_parent_firestore_auth_and_csrf_still_guard_deletes(rig, tmp_path):
    adapter, client_data = rig
    client = api(adapter, tmp_path)
    body = {"confirm_name": "測試牛奶", "expected_location_id": 7}
    before = copy.deepcopy(client_data.data)
    assert client.request("DELETE", "/api/items/11", json=body, headers={"origin": "https://evil.test"}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/locations").status_code == 401
    assert client.request("DELETE", "/api/items/11", json=body).status_code == 401
    assert client_data.data == before


def test_cloud_failure_is_generic_and_no_fallback(rig, tmp_path, monkeypatch):
    adapter, client_data = rig
    client = api(adapter, tmp_path)
    monkeypatch.setattr(app_module, "Grocy", lambda *args: pytest.fail("must never fall back to Grocy"))
    client_data.failure = "read"
    for path in ("/api/state", "/api/locations"):
        response = client.get(path)
        assert response.status_code == 503 and "private" not in response.text and "credential" not in response.text
    assert client.get("/health").status_code == 503


def test_backend_selection_and_injected_adapter_priority(rig, tmp_path, monkeypatch):
    adapter, _ = rig
    monkeypatch.setenv("INVENTORY_BACKEND", "firestore")
    monkeypatch.setattr(app_module, "FirestoreInventory", lambda: adapter)
    selected = app_module.create_app(ai=NoAI(), db_path=tmp_path / "selected.db", password="test", secret="x" * 40)
    assert selected.state.grocy is adapter
    injected = object()
    selected = app_module.create_app(grocy=injected, ai=NoAI(), db_path=tmp_path / "injected.db", password="test", secret="x" * 40)
    assert selected.state.grocy is injected
    monkeypatch.setenv("INVENTORY_BACKEND", "typo")
    with pytest.raises(ValueError, match="Unsupported inventory backend"):
        app_module.create_app(ai=NoAI(), db_path=tmp_path / "typo.db", password="test", secret="x" * 40)
