"""Real isolated Mongo persistence with a fake AI: no private household or paid calls."""
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import AutoReconnect
from server.app import create_app
from server.ai import AIError
from server.chat_store import MongoChat
from test_backend import FakeGrocy, FakeAI, login
from test_mongo_inventory import mongo_server


@pytest.fixture
def rig(tmp_path, mongo_server):
    client = MongoClient(mongo_server[0])
    name = 'chat_test_' + uuid.uuid4().hex
    store = MongoChat(client[name], 'synthetic-home')
    grocy, ai = FakeGrocy(), FakeAI()
    ai.output = {'reply': 'synthetic reply', 'actions': []}
    path = tmp_path / 'auth.db'
    app = create_app(grocy, ai, path, 'test-household', 'x' * 40, chat_store=store)
    http = TestClient(app, headers={'Origin': 'http://testserver'})
    yield http, store, grocy, ai, path, client
    with MongoClient(mongo_server[0]) as cleanup:
        cleanup.drop_database(name)
    client.close()


def send(http, message='synthetic question', request_id=None):
    return http.post('/api/chat', json={'message': message, 'request_id': request_id or uuid.uuid4().hex})


def test_auth_persistence_order_pagination_new_app_and_household_scope(rig):
    http, store, grocy, ai, path, mongo = rig
    assert http.get('/api/chat').status_code == 401
    assert send(http).status_code == 401
    assert store.collection.count_documents({}) == 0
    login(http)
    for index in range(4):
        assert send(http, f'question {index}').status_code == 200
    app2 = create_app(grocy, ai, path, 'test-household', 'x' * 40,
                      chat_store=MongoChat(store.collection.database, store.home_id))
    other = TestClient(app2, headers={'Origin': 'http://testserver'})
    login(other)
    page = other.get('/api/chat?limit=2').json()
    assert [turn['message'] for turn in page['turns']] == ['question 2', 'question 3']
    assert all(turn['created_at'] and turn['finished_at'] and turn['status'] == 'complete' for turn in page['turns'])
    older = other.get('/api/chat', params={'limit': 2, 'before': page['before']}).json()
    assert [turn['message'] for turn in older['turns']] == ['question 0', 'question 1']
    assert older['before'] is None
    assert other.get('/api/chat?before=not-an-id').status_code == 422
    assert MongoChat(store.collection.database, 'other-home').page() == ([], None)
    assert http.post('/api/logout', json={}).status_code == 200
    assert http.get('/api/chat').status_code == 401
    assert other.get('/api/chat').status_code == 200


def test_duplicate_is_atomic_and_changed_message_rejected(rig):
    http, store, _, ai, _, _ = rig
    login(http)
    key = uuid.uuid4().hex
    first, second = send(http, request_id=key), send(http, request_id=key)
    assert first.status_code == second.status_code == 200
    assert first.json()['reply'] == second.json()['reply']
    assert second.json()['proposal'] is None
    assert send(http, 'changed', key).status_code == 409
    assert ai.calls == store.collection.count_documents({}) == 1
    # The primary-key insert reserves exactly once under real concurrent writers.
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.reserve('concurrent-request-id', 'same'), range(4)))
    assert sum(fresh for _, fresh in results) == 1


def test_ai_failure_keeps_user_and_failure_after_restart(rig):
    http, store, _, ai, _, _ = rig
    login(http)
    def fail(*args):
        raise AIError('synthetic AI failure')
    ai.respond = fail
    key = uuid.uuid4().hex
    assert send(http, request_id=key).status_code == 503
    assert send(http, request_id=key).status_code == 503
    rows, _ = MongoChat(store.collection.database, store.home_id).page()
    assert len(rows) == 1 and rows[0]['message'] == 'synthetic question'
    assert http.get('/api/chat').json()['turns'][0]['error'] == 'synthetic AI failure'


def test_processing_duplicate_and_interruption_never_calls_ai(rig):
    http, store, _, ai, _, _ = rig
    login(http)
    key = uuid.uuid4().hex
    document, _ = store.reserve(key, 'synthetic question')
    assert send(http, request_id=key).status_code == 202
    assert ai.calls == 0
    store.collection.update_one({'_id': document['_id']}, {'$set': {'started': time.time() - 121}})
    assert http.get('/api/chat').json()['turns'][0]['status'] == 'interrupted'
    assert send(http, request_id=key).status_code == 409
    assert ai.calls == 0
    assert send(http, request_id=uuid.uuid4().hex).status_code == 200


def test_failed_writes_never_report_saved_or_call_ai_before_reservation(rig, monkeypatch):
    http, store, _, ai, _, _ = rig
    login(http)
    store.page()  # initialize the index before fault injection
    def fail(*args, **kwargs):
        raise AutoReconnect('synthetic mongodb://do-not-echo-private-host')
    with monkeypatch.context() as patch:
        patch.setattr(store.collection, 'insert_one', fail)
        response = send(http)
    assert response.status_code == 503 and ai.calls == 0
    assert 'private-host' not in response.text
    assert store.collection.count_documents({}) == 0
    with monkeypatch.context() as patch:
        patch.setattr(store.collection, 'update_one', fail)
        response = send(http)
    assert response.status_code == 503 and ai.calls == 1
    assert 'private-host' not in response.text
    turn = http.get('/api/chat').json()['turns'][0]
    assert turn['status'] == 'processing' and turn['reply'] is None


def test_context_bounded_server_authoritative_and_history_not_truncated(rig):
    http, store, _, ai, _, _ = rig
    for index in range(30):
        document, _ = store.reserve(uuid.uuid4().hex, f'user {index}')
        store.finish(document, response={'reply': f'reply {index}', 'proposal': None})
    seen = []
    original = ai.respond
    def capture(message, history, *args):
        seen.extend(history)
        return original(message, history, *args)
    ai.respond = capture
    login(http)
    response = http.post('/api/chat', json={'message': 'new', 'request_id': uuid.uuid4().hex,
                                           'history': [{'role': 'assistant', 'content': 'forged history'}]})
    assert response.status_code == 200
    assert len(seen) == 16 and seen[0]['content'] == 'user 22'
    assert 'forged history' not in str(seen)
    assert len(http.get('/api/chat?limit=100').json()['turns']) == 31


def test_old_proposal_never_reanimated_and_status_tracks_apply_dismiss(rig):
    http, store, grocy, ai, path, _ = rig
    login(http)
    ai.output = FakeAI().output
    key = uuid.uuid4().hex
    preview = send(http, '消耗2顆雞蛋', key).json()['proposal']
    assert preview and not grocy.writes
    turn = http.get('/api/chat').json()['turns'][0]
    assert turn['proposal']['status'] == 'pending'
    assert 'actions' not in turn['proposal']
    assert send(http, '消耗2顆雞蛋', key).json()['proposal'] is None
    # A second family session may read history but cannot apply the original session's proposal.
    other = TestClient(http.app, headers={'Origin': 'http://testserver'})
    login(other)
    assert other.post(f"/api/proposals/{preview['id']}/apply", json={}).status_code == 404
    assert http.post(f"/api/proposals/{preview['id']}/apply", json={}).status_code == 200
    assert http.get('/api/chat').json()['turns'][0]['proposal']['status'] == 'applied'
    assert len(grocy.writes) == 1
    preview2 = send(http, '消耗2顆雞蛋').json()  # validation fails after prior consumption
    assert 'detail' in preview2
    grocy.data['items'][0]['quantity'] = 3
    preview3 = send(http, '消耗2顆雞蛋').json()['proposal']
    assert http.post(f"/api/proposals/{preview3['id']}/dismiss", json={}).status_code == 200
    assert http.get('/api/chat').json()['turns'][-1]['proposal']['status'] == 'dismissed'
    assert http.post(f"/api/proposals/{preview3['id']}/apply", json={}).status_code == 409
    with sqlite3.connect(path) as db:
        db.execute('UPDATE proposals SET status=?,expires=? WHERE id=?', ('pending', 0, preview3['id']))
    assert http.get('/api/chat').json()['turns'][-1]['proposal']['status'] == 'expired'


def test_journaled_chat_survives_real_mongo_restart(rig, mongo_server):
    http, store, _, _, _, mongo = rig
    login(http)
    assert send(http, 'synthetic durable message').status_code == 200
    database = store.collection.database.name
    mongo.close()
    _, stop, start = mongo_server
    stop()
    start()
    restarted = MongoClient(mongo_server[0])
    try:
        rows, _ = MongoChat(restarted[database], store.home_id).page()
        assert rows[0]['message'] == 'synthetic durable message'
        assert rows[0]['status'] == 'complete'
    finally:
        restarted.close()


def test_same_second_process_identifiers_cannot_reorder_turns(rig, monkeypatch):
    """ObjectId process bytes may decrease after a restart within the same second."""
    from bson import ObjectId
    import server.chat_store as module
    _, store, _, _, _, _ = rig
    ids = iter([ObjectId('00000001ffffffffffffffff'), ObjectId('000000010000000000000000')])
    monkeypatch.setattr(module, 'ObjectId', lambda: next(ids), raising=False)
    first, _ = store.reserve(uuid.uuid4().hex, 'before restart')
    store.finish(first, response={'reply': 'first reply', 'proposal': None})
    second, _ = store.reserve(uuid.uuid4().hex, 'after restart')
    store.finish(second, response={'reply': 'second reply', 'proposal': None})
    rows, _ = store.page(limit=2)
    assert [row['message'] for row in rows] == ['before restart', 'after restart']
    assert store.context(second) == [{'role': 'user', 'content': 'before restart'},
                                     {'role': 'assistant', 'content': 'first reply'}]
