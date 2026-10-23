"""Persistent conversation boundaries with isolated real Mongo and synthetic AI/images."""
import copy
import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from bson import json_util
from server import contracts as c
from server.chat_store import MongoChat
from test_backend import login
from test_chat_persistence import rig
from test_chat_images_context import photo_rig, image
from test_mongo_inventory import mongo_server


def create(http):
    response = http.post(c.CHAT_CONVERSATIONS, json={})
    assert response.status_code == 200
    return response.json()['conversation_id']


def send(http, cid=None, message='synthetic request', images=None, key=None):
    body = {'message': message, 'images': images or [], 'request_id': key or uuid.uuid4().hex}
    if cid is not None: body['conversation_id'] = cid
    return http.post(c.CHAT, json=body)


def test_legacy_projection_is_non_destructive_and_old_clients_keep_all_rows(photo_rig):
    http, store, _, ai, _, _ = photo_rig
    assert send(http, message='old text', images=[image()]).status_code == 200
    # Represent an actual pre-upgrade record with its original missing field.
    store.collection.update_many({}, {'$unset': {'conversation_id': ''}})
    before = list(store.collection.find({}))
    expected = hashlib.sha256(json_util.dumps(before, sort_keys=True).encode()).hexdigest()
    listing = http.get(c.CHAT_CONVERSATIONS).json()
    assert listing['conversations'][0]['title'] == '先前的對話'
    assert listing['conversations'][0]['conversation_id'] == c.CHAT_LEGACY
    assert http.get(c.CHAT).json()['turns'] == http.get(c.CHAT, params={'conversation_id': c.CHAT_LEGACY}).json()['turns']
    new = create(http)
    assert http.get(c.CHAT, params={'conversation_id': new}).json()['turns'] == []
    assert expected == hashlib.sha256(json_util.dumps(list(store.collection.find({})), sort_keys=True).encode()).hexdigest()
    turn = http.get(c.CHAT).json()['turns'][0]
    assert http.get(turn['images'][0]['url']).status_code == 200
    assert store.collection.count_documents({}) == 1 and len(ai.calls) == 1


def test_conversation_history_and_recent_photos_are_isolated(photo_rig):
    http, store, _, ai, _, _ = photo_rig
    a, b = create(http), create(http)
    assert send(http, a, message='A picture', images=[image()]).status_code == 200
    assert send(http, b, message='B first').status_code == 200
    assert ai.calls[-1]['history'] == [] and ai.calls[-1]['images'] == []
    assert send(http, a, message='A followup').status_code == 200
    assert [row['content'] for row in ai.calls[-1]['history'] if row['role'] == 'user'] == ['A picture']
    assert len(ai.calls[-1]['images']) == 1
    assert [row['message'] for row in http.get(c.CHAT, params={'conversation_id': b}).json()['turns']] == ['B first']
    assert http.get(c.CHAT).json()['turns'] == []
    titles = {row['conversation_id']: row['title'] for row in http.get(c.CHAT_CONVERSATIONS).json()['conversations']}
    assert titles[a] == 'A picture' and titles[b] == 'B first'
    other = MongoChat(store.collection.database, 'other-home')
    assert other.conversation_page()[0] == []


def test_cross_conversation_duplicate_key_cannot_leak_response_and_permissions(photo_rig):
    http, store, _, ai, _, _ = photo_rig
    a, b, key = create(http), create(http), uuid.uuid4().hex
    first = send(http, a, message='same message', key=key)
    assert first.status_code == 200
    assert send(http, a, message='same message', key=key).status_code == 200
    duplicate = send(http, b, message='same message', key=key)
    assert duplicate.status_code == 409 and 'reply' not in duplicate.json()
    assert send(http, 'missing-conversation').status_code == 404
    assert http.get(c.CHAT, params={'conversation_id': 'missing-conversation'}).status_code == 404
    assert send(http, '../../bad').status_code == 422
    assert len(ai.calls) == 1 and store.collection.count_documents({}) == 1
    http.post(c.LOGOUT, json={})
    assert http.get(c.CHAT_CONVERSATIONS).status_code == 401
    assert http.post(c.CHAT_CONVERSATIONS, json={}).status_code == 401
    assert http.get(c.CHAT, params={'conversation_id': a}).status_code == 401


def test_conversations_and_turn_pagination_and_image_title_persist(photo_rig):
    http, store, _, ai, _, _ = photo_rig
    ids = [create(http) for _ in range(5)]
    assert send(http, ids[0], message='', images=[image()]).status_code == 200
    for index in range(4): assert send(http, ids[1], message=f'B {index}').status_code == 200
    found, cursor = [], None
    while True:
        params = {'limit': 2}
        if cursor: params['before'] = cursor
        page = http.get(c.CHAT_CONVERSATIONS, params=params).json()
        found.extend(page['conversations']); cursor = page['before']
        if not cursor: break
    assert {row['conversation_id'] for row in found} == set(ids) and len(found) == 5
    assert next(row for row in found if row['conversation_id'] == ids[0])['title'] == '圖片對話'
    page = http.get(c.CHAT, params={'conversation_id': ids[1], 'limit': 2}).json()
    older = http.get(c.CHAT, params={'conversation_id': ids[1], 'limit': 2, 'before': page['before']}).json()
    assert [row['message'] for row in older['turns'] + page['turns']] == ['B 0', 'B 1', 'B 2', 'B 3']
    assert http.get(c.CHAT_CONVERSATIONS, params={'before': '%%%'}).status_code == 422
    fresh = MongoChat(store.collection.database, store.home_id)
    assert len(fresh.conversation_page()[0]) == 5
    assert [row['message'] for row in fresh.page(conversation_id=ids[1])[0]] == ['B 0', 'B 1', 'B 2', 'B 3']


def test_pending_a_and_b_are_independent_even_when_a_finishes_later(photo_rig):
    http, store, _, ai, _, _ = photo_rig
    a, b = create(http), create(http)
    started, release = Event(), Event()
    respond = ai.respond
    def delayed(message, history, state, today, images=None):
        if message == 'A waiting':
            started.set()
            assert release.wait(10)
        return respond(message, history, state, today, images=images)
    ai.respond = delayed
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(send, http, a, 'A waiting')
        assert started.wait(5)
        assert send(http, b, 'B active').status_code == 200
        assert [row['message'] for row in http.get(c.CHAT, params={'conversation_id': b}).json()['turns']] == ['B active']
        release.set(); assert pending.result(timeout=10).status_code == 200
    assert [row['message'] for row in http.get(c.CHAT, params={'conversation_id': a}).json()['turns']] == ['A waiting']
    assert [row['message'] for row in http.get(c.CHAT, params={'conversation_id': b}).json()['turns']] == ['B active']
