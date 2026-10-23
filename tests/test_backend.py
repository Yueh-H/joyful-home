"""API regression tests with explicit fake adapters; not a real Grocy/Gemini E2E."""
import copy
import pytest
from fastapi.testclient import TestClient
from server.app import create_app, quantity_grounded
from server.contracts import AIResponse
from server.grocy import Grocy, GrocyError
from server.ai import Gemini

class FakeGrocy:
    def __init__(self):
        self.data = {"items": [{"id": 1, "name": "雞蛋", "category": "food", "quantity": 3, "unit": "顆", "location": "冰箱", "min_quantity": 2,
            "best_before": "2026-10-24", "opened": False, "model": "", "description": "", "interval_days": None, "last_maintained": None, "next_due": None}],
            "shopping": [{"id": 7, "item_id": 1, "name": "雞蛋", "amount": 2, "unit": "顆"}]}
        self.writes = []
    def state(self):
        return copy.deepcopy(self.data)
    def request(self, *args):
        if args[0] == 'DELETE':
            self.writes.append(args)
            self.data['items'] = []
        return {}
    def action(self, action, today):
        self.writes.append(copy.deepcopy(action))
        item = self.data["items"][0]
        if action["type"] == "consume": item["quantity"] -= action["amount"]
        if action["type"] in ("purchase", "shopping_complete"): item["quantity"] += action["amount"]
        if action["type"] == "maintain": item["last_maintained"] = today
        if action["type"] == "shopping_complete": self.data["shopping"] = []
    def patch(self, pid, patch, item):
        self.writes.append(patch)
        self.data["items"][0].update(patch)
    def create(self, data):
        self.writes.append(data)
        return 2

class FakeAI:
    enabled = True
    def __init__(self):
        self.output = {"reply": "這是預覽，尚未寫入。", "summary": "消耗 2 顆雞蛋", "actions": [{"type": "consume", "item_id": 1, "amount": 2, "amount_source": "2"}]}
        self.calls = 0
    def respond(self, message, history, state, today):
        self.calls += 1
        return AIResponse.model_validate(self.output)

@pytest.fixture

def rig(tmp_path):
    grocy, ai = FakeGrocy(), FakeAI()
    app = create_app(grocy, ai, tmp_path / 'app.db', 'test-household', 'x' * 40)
    client = TestClient(app, headers={"Origin": "http://testserver"})
    return client, grocy, ai

def login(client):
    assert client.post('/api/login', json={"password": "test-household"}).status_code == 200

def test_auth_and_csrf(rig):
    client, grocy, _ = rig
    for path in ['/api/state', '/api/export']:
        assert client.get(path).status_code == 401
    assert client.post('/api/chat', json={"message": "看庫存"}).status_code == 401
    assert client.post('/api/login', json={"password": "wrong"}).status_code == 401
    assert client.post('/api/login', json={"password": "test-household"}, headers={"origin": "https://evil.example"}).status_code == 403
    login(client)
    assert client.get('/api/state').status_code == 200
    assert 'HttpOnly' in client.post('/api/login', json={"password": "test-household"}).headers['set-cookie']
    assert client.post('/api/actions', json={"type": "consume", "item_id": 1, "amount": 1}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.post('/api/logout', json={}).status_code == 200
    assert client.get('/api/export').status_code == 401
    assert not grocy.writes

@pytest.mark.parametrize('amount', [4, 0, -1, 'NaN', 'Infinity'])
def test_invalid_or_excess_consumption(rig, amount):
    client, grocy, _ = rig
    login(client)
    assert client.post('/api/actions', json={"type": "consume", "item_id": 1, "amount": amount}).status_code in (409, 422)
    assert not grocy.writes

@pytest.mark.parametrize('patch', [{"name": None}, {"quantity": None}, {"quantity": 'NaN'}, {"unit": None}])
def test_null_patch_rejected(rig, patch):
    client, grocy, _ = rig
    login(client)
    assert client.patch('/api/items/1', json=patch).status_code == 422
    assert not grocy.writes

def test_preview_then_apply_once(rig):
    client, grocy, ai = rig
    login(client)
    preview = client.post('/api/chat', json={"message": "消耗2顆雞蛋"}).json()['proposal']
    assert preview and not grocy.writes
    url = f"/api/proposals/{preview['id']}/apply"
    assert client.post(url, json={}).status_code == 200
    assert grocy.data['items'][0]['quantity'] == 1
    assert client.post(url, json={}).status_code == 409
    assert len(grocy.writes) == 1 and ai.calls == 1

def test_confirmation_summary_uses_validated_actions_not_model_prose(rig):
    client, grocy, ai = rig
    login(client)
    ai.output['summary'] = '買入 100 瓶鮮奶'
    ai.output['reply'] = '已完成買入鮮奶'
    result = client.post('/api/chat', json={'message': '消耗2顆雞蛋'}).json()
    assert result['proposal']['summary'] == '消耗「雞蛋」2 顆'
    assert '鮮奶' not in result['reply'] and '尚未寫入' in result['reply']
    assert not grocy.writes

def test_stale_proposal_rejected(rig):
    client, grocy, _ = rig
    login(client)
    pid = client.post('/api/chat', json={"message": "消耗2顆雞蛋"}).json()['proposal']['id']
    grocy.data['items'][0]['quantity'] = 2
    assert client.post(f'/api/proposals/{pid}/apply', json={}).status_code == 409
    assert not grocy.writes

def test_multi_action_cumulative_overconsumption(rig):
    client, grocy, ai = rig
    login(client)
    ai.output['actions'] *= 2
    assert client.post('/api/chat', json={"message": "各消耗2顆雞蛋"}).status_code == 409
    assert not grocy.writes

def test_unknown_quantity_clarifies_and_readonly_never_writes(rig):
    client, grocy, ai = rig
    login(client)
    assert client.post('/api/chat', json={"message": "雞蛋用掉一些"}).json()['proposal'] is None
    ai.output = {"reply": "可以做煎蛋。", "actions": []}
    assert client.post('/api/chat', json={"message": "用現有食材建議食譜"}).json()['proposal'] is None
    assert not grocy.writes

def test_shopping_id_and_product_relation(rig):
    client, grocy, _ = rig
    login(client)
    for shopping_id in [None, 8]:
        payload = {"type": "shopping_complete", "item_id": 1, "amount": 2, "shopping_id": shopping_id}
        assert client.post('/api/actions', json=payload).status_code == 409
    assert not grocy.writes
    payload['shopping_id'] = 7
    assert client.post('/api/actions', json=payload).status_code == 200
    assert grocy.data['items'][0]['quantity'] == 5 and not grocy.data['shopping']

def test_manual_idempotency_key(rig):
    client, grocy, _ = rig
    login(client)
    payload = {"type": "purchase", "item_id": 1, "amount": 2}
    headers = {"Idempotency-Key": "request-0123456789"}
    first = client.post('/api/actions', json=payload, headers=headers)
    second = client.post('/api/actions', json=payload, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and len(grocy.writes) == 1
    assert client.post('/api/actions', json={**payload, "amount": 1}, headers=headers).status_code == 409

def test_missing_ai_key_manual_still_works(rig):
    client, grocy, _ = rig
    client.app.state.ai = Gemini(key='')
    login(client)
    assert client.get('/api/state').json()['ai']['enabled'] is False
    assert client.post('/api/chat', json={"message": "你好"}).status_code == 503
    assert client.post('/api/actions', json={"type": "maintain", "item_id": 1}).status_code == 200

def test_grocy_failure_not_empty_state(rig):
    client, grocy, _ = rig
    login(client)
    def fail(): raise GrocyError('Grocy 斷線')
    grocy.state = fail
    response = client.get('/api/state')
    assert response.status_code == 502 and response.json() == {'detail': 'Grocy 斷線'}

def test_quantity_source_boundaries():
    assert quantity_grounded('三', '用掉三顆', 3)
    assert quantity_grounded('半', '用了半瓶', 0.5)
    assert not quantity_grounded('2', '用了12顆', 2)
    assert not quantity_grounded('2', '用了兩顆', 2)

def test_delete_zero_stock_only_and_no_shopping(rig):
    client, grocy, _ = rig
    assert client.request('DELETE', '/api/items/1', json={}).status_code == 401
    login(client)
    assert client.request('DELETE', '/api/items/1', json={}, headers={'origin': 'https://evil.example'}).status_code == 403
    assert client.request('DELETE', '/api/items/1', json={}).status_code == 409
    grocy.data['items'][0]['quantity'] = 0
    assert client.request('DELETE', '/api/items/1', json={}).status_code == 409
    assert not grocy.writes
    grocy.data['shopping'] = []
    assert client.request('DELETE', '/api/items/1', json={}).status_code == 200
    assert not grocy.data['items']
    assert '已移除零庫存物品' in client.get('/api/state').json()['activity'][0]['message']

def test_shopping_failure_undoes_transaction():
    g = Grocy('http://unused', 'not-a-key')
    calls = []
    def request(method, route, data=None):
        calls.append((method, route))
        if route.endswith('/add'): return [{"transaction_id": "purchase-123"}]
        if method == 'DELETE': raise GrocyError('failure')
        if route == 'objects/shopping_list': return [{"id": 7}]
    g.request = request
    with pytest.raises(GrocyError, match='已撤銷'):
        g.action({'type': 'shopping_complete', 'item_id': 1, 'shopping_id': 7, 'amount': 2}, '2026-10-23')
    assert ('POST', 'stock/transactions/purchase-123/undo') in calls

def test_full_patch_skips_unchanged_inventory_booking():
    g = Grocy('http://unused', 'not-a-key')
    g.ready = True
    calls = []
    def request(method, route, data=None):
        calls.append((method, route, data))
        if route == 'stock/products/1/entries':
            return [{"id": 9, "amount": 3, "best_before_date": "2999-12-31", "open": 0, "location_id": 1}]
        return []
    g.request = request
    g.patch(1, {'quantity': 3, 'model': 'QA-10', 'best_before': '2026-11-01'}, {'quantity': 3})
    assert not any(route.endswith('/inventory') for _, route, _ in calls)
    assert any(route == 'stock/entry/9' and data['best_before_date'] == '2026-11-01' for _, route, data in calls)

def test_state_uses_nearest_batch_opened_and_keeps_zero_stock_products():
    g = Grocy('http://unused', 'not-a-key')
    entities = {
        'products': [{"id": 1, "name": "蛋", "qu_id_stock": 1, "location_id": 1}, {"id": 2, "name": "空盒", "qu_id_stock": 1, "location_id": 1}],
        'quantity_units': [{"id": 1, "name": "顆"}], 'locations': [{"id": 1, "name": "冰箱"}], 'product_groups': [],
        'stock': [{"id": 4, "product_id": 1, "amount": 3, "best_before_date": '2026-12-01', "open": 1},
                  {"id": 5, "product_id": 1, "amount": 2, "best_before_date": '2026-11-01', "open": 0}], 'shopping_list': []}
    g.objects = lambda name: entities[name]
    items = g.state()['items']
    assert items[0]['quantity'] == 5 and items[0]['best_before'] == '2026-11-01' and items[0]['opened'] is False
    assert items[1]['quantity'] == 0

def test_http204_empty_household_bootstrap_and_state():
    import httpx
    writes = []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(204)
        writes.append((request.method, request.url.path))
        return httpx.Response(201, json={'created_object_id': len(writes)})
    g = Grocy('http://empty-grocy', 'not-a-key')
    g.client = httpx.Client(base_url='http://empty-grocy/api/', transport=httpx.MockTransport(handler))
    assert g.state() == {'items': [], 'shopping': []}
    assert not writes  # Reading a first-use household never bootstraps or seeds.
    g.bootstrap()
    assert len(writes) == 10  # Four metadata fields, four groups, one unit, one location.
    g.bootstrap()
    assert len(writes) == 10  # Bootstrap is idempotent within the adapter.
    assert g.state() == {'items': [], 'shopping': []}
    g.patch(1, {'best_before': '2026-12-01'}, {'quantity': 0})
    assert len(writes) == 10  # No stock entry exists, so no batch update is attempted.

@pytest.mark.parametrize('path', ['objects/products', 'stock/products/1/entries'])
def test_malformed_collection_is_not_silently_empty(path):
    import httpx
    g = Grocy('http://invalid-grocy', 'not-a-key')
    g.client = httpx.Client(base_url='http://invalid-grocy/api/', transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'error': 'invalid collection'})))
    with pytest.raises(GrocyError, match='清單格式不正確'):
        g.read_list(path)
