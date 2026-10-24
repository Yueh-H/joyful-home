"""Rename/delete only metadata: isolated Mongo, synthetic chat/photos, no real provider."""
import copy
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from pymongo.errors import AutoReconnect
from server import contracts as c
from server.chat_store import MongoChat, ChatStoreError
from test_chat_conversations import create, send
from test_chat_persistence import rig
from test_chat_images_context import photo_rig, image
from test_mongo_inventory import mongo_server


def path(cid):
    return c.CHAT_CONVERSATION.format(conversation_id=cid)


def delete(http, cid):
    return http.request('DELETE', path(cid), json={})


def test_rename_trim_validation_and_custom_title_survives_first_send_and_new_worker(photo_rig):
    http, store, _, ai, _, _ = photo_rig
    cid = create(http)
    assert http.patch(path(cid), json={'title': '  自訂名稱 <b>  '}).json()['title'] == '自訂名稱 <b>'
    for title in ['', ' \n\t ', 'x' * 81]:
        assert http.patch(path(cid), json={'title': title}).status_code == 422
    assert send(http, cid, 'first auto title must not win').status_code == 200
    fresh = MongoChat(store.collection.database, store.home_id)
    assert fresh.conversation_page()[0][0]['title'] == '自訂名稱 <b>'
    assert fresh.conversations.find_one({'conversation_id': cid})['custom_title'] is True
    assert len(ai.calls) == 1


def test_legacy_override_never_duplicates_or_rewrites_original_turns_and_tombstone_persists(photo_rig):
    http, store, _, _, _, _ = photo_rig
    assert send(http, message='old legacy message', images=[image()]).status_code == 200
    store.collection.update_many({}, {'$unset': {'conversation_id': ''}})
    original = list(store.collection.find({}))
    assert http.patch(path(c.CHAT_LEGACY), json={'title': '舊對話新名稱'}).status_code == 200
    assert http.patch(path(c.CHAT_LEGACY), json={'title': '再改名'}).status_code == 200
    rows = http.get(c.CHAT_CONVERSATIONS).json()['conversations']
    assert len(rows) == 1 and rows[0]['title'] == '再改名'
    assert list(store.collection.find({})) == original
    sequence = store.sequences.find_one({})
    assert delete(http, c.CHAT_LEGACY).status_code == 200
    assert list(store.collection.find({})) == original
    assert store.sequences.find_one({}) == sequence
    fresh = MongoChat(store.collection.database, store.home_id)
    assert fresh.conversation_page()[0] == []
    assert fresh.conversations.count_documents({'conversation_id': c.CHAT_LEGACY}) == 1
    assert 'deleted_at' in fresh.conversations.find_one({'conversation_id': c.CHAT_LEGACY})
    assert send(http, message='old client default cannot resurrect').status_code == 410
    assert http.get(c.CHAT).status_code == 410
    with pytest.raises(ChatStoreError) as exc: fresh.page()
    assert exc.value.status_code == 410


def test_deleted_conversation_denies_images_context_cached_replay_and_keeps_other_conversation(photo_rig):
    http, store, _, ai, _, _ = photo_rig
    a, b, key = create(http), create(http), uuid.uuid4().hex
    assert send(http, a, 'A photo', images=[image()], key=key).status_code == 200
    assert send(http, b, 'B survives').status_code == 200
    row = http.get(c.CHAT, params={'conversation_id': a}).json()['turns'][0]
    document = store.collection.find_one({'request_id': key})
    before = copy.deepcopy(document)
    sequence = store.sequences.find_one({})
    assert delete(http, a).status_code == 200
    assert store.collection.find_one({'request_id': key}) == before
    assert store.sequences.find_one({}) == sequence
    assert http.get(row['images'][0]['url']).status_code == 410
    assert http.get(c.CHAT, params={'conversation_id': a}).status_code == 410
    assert send(http, a, 'A photo', images=[image()], key=key).status_code == 410
    for method in [lambda: store.context(document), lambda: store.model_images(document, []), lambda: store.model_images(document, [{'data': b'x'}] * 4)]:
        with pytest.raises(ChatStoreError) as exc: method()
        assert exc.value.status_code == 410
    assert http.patch(path(a), json={'title': 'revive?'}).status_code == 410
    assert [turn['message'] for turn in http.get(c.CHAT, params={'conversation_id': b}).json()['turns']] == ['B survives']
    assert len(ai.calls) == 2


def test_delete_while_provider_pending_cannot_finish_or_recreate_metadata(photo_rig):
    http, store, _, ai, _, _ = photo_rig
    cid = create(http)
    entered, release = Event(), Event()
    respond = ai.respond
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return respond(*args, **kwargs)
    ai.respond = slow
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(send, http, cid, 'pending first message')
        assert entered.wait(5)
        original = store.collection.find_one({'conversation_id': cid})
        assert delete(http, cid).status_code == 200
        metadata = store.conversations.find_one({'conversation_id': cid})
        release.set()
        assert pending.result(timeout=10).status_code == 410
    assert store.collection.find_one({'conversation_id': cid}) == original
    assert store.conversations.find_one({'conversation_id': cid}) == metadata
    assert http.get(c.CHAT_CONVERSATIONS).json()['conversations'] == []


def test_mutation_auth_ownership_and_driver_failures_do_not_report_success(photo_rig, monkeypatch):
    http, store, _, _, _, _ = photo_rig
    cid = create(http)
    other = MongoChat(store.collection.database, 'other-home').create_conversation()['conversation_id']
    assert http.patch(path(other), json={'title': 'not mine'}).status_code == 404
    assert delete(http, other).status_code == 404
    original = store.conversations.find_one({'conversation_id': cid})
    def fail(*args, **kwargs): raise AutoReconnect('synthetic private driver failure')
    with monkeypatch.context() as patch:
        patch.setattr(store.conversations, 'find_one_and_update', fail)
        response = http.patch(path(cid), json={'title': 'must not claim success'})
        assert response.status_code == 503 and 'synthetic' not in response.text
    with monkeypatch.context() as patch:
        patch.setattr(store.conversations, 'update_one', fail)
        assert delete(http, cid).status_code == 503
    assert store.conversations.find_one({'conversation_id': cid}) == original
    http.post(c.LOGOUT, json={})
    assert http.patch(path(cid), json={'title': 'denied'}).status_code == 401
    assert delete(http, cid).status_code == 401
