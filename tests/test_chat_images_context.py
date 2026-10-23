"""Synthetic raster/SDK tests with real isolated Mongo; never use private photos or real AI."""
import base64
import copy
from io import BytesIO
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pymongo import MongoClient
from pymongo.errors import AutoReconnect
from server import contracts as c
from server.ai import AIError, Gemini
from server.app import create_app
from server.chat_store import MongoChat
from server.mongo_inventory import MongoInventory
from scripts.mongo_household import import_snapshot
from test_backend import FakeGrocy, login
from test_chat_persistence import rig
from test_mongo_inventory import mongo_server, sample


class ImageAI:
    enabled = True
    def __init__(self):
        self.calls = []
        self.fail = False
    def respond(self, message, history, state, today, images=None):
        self.calls.append({'message': message, 'history': copy.deepcopy(history), 'state': copy.deepcopy(state), 'images': copy.deepcopy(images or [])})
        if self.fail:
            raise AIError('synthetic image failure')
        return c.AIResponse(reply='synthetic image answer', actions=[])


def raster(color='red', size=(24, 16), format='PNG', exif=False):
    output = BytesIO()
    meta = Image.Exif()
    meta[271] = 'synthetic-private-metadata'
    Image.new('RGB', size, color).save(output, format=format, **({'exif': meta} if exif else {}))
    return output.getvalue()


def image(raw=None, mime='image/png'):
    return {'mime_type': mime, 'data': base64.b64encode(raw if raw is not None else raster()).decode()}


def send(http, images=None, message='', key=None):
    return http.post(c.CHAT, json={'request_id': key or uuid.uuid4().hex, 'message': message, 'images': images or []})


@pytest.fixture
def photo_rig(rig):
    http, store, grocy, _, path, mongo = rig
    ai = ImageAI()
    http.app.state.ai = ai
    login(http)
    return http, store, grocy, ai, path, mongo


def test_image_only_persists_atomically_authenticated_and_survives_new_app(photo_rig):
    http, store, grocy, ai, path, mongo = photo_rig
    key = uuid.uuid4().hex
    assert send(http, [image()], key=key).status_code == 200
    row = http.get(c.CHAT).json()['turns'][0]
    assert row['message'] == '' and row['status'] == 'complete' and len(row['images']) == 1
    assert 'data' not in str(row['images'])
    url = row['images'][0]['url']
    response = http.get(url)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['cross-origin-resource-policy'] == 'same-origin'
    assert Image.open(BytesIO(response.content)).size == (24, 16)
    assert http.get(url, headers={'sec-fetch-site': 'same-site'}).status_code == 403
    assert http.get(url, headers={'origin': 'https://foreign.example'}).status_code == 403
    app2 = create_app(grocy, ai, path, 'test-household', 'x' * 40,
                      chat_store=MongoChat(store.collection.database, store.home_id))
    client2 = TestClient(app2, headers={'Origin': 'http://testserver'})
    assert client2.get(url).status_code == 401
    login(client2)
    assert client2.get(url).content == response.content
    app2.state.chat = MongoChat(store.collection.database, 'other-home')
    assert client2.get(url).status_code == 404
    assert store.collection.count_documents({}) == 1
    assert len(ai.calls[0]['images']) == 1


def test_image_retry_equality_and_failed_reply_keeps_image(photo_rig):
    http, store, _, ai, _, _ = photo_rig
    key = uuid.uuid4().hex
    assert send(http, [image()], key=key).status_code == 200
    assert send(http, [image()], key=key).status_code == 200
    assert len(ai.calls) == 1
    assert send(http, [image(raster('blue'))], key=key).status_code == 409
    assert send(http, [image()], message='changed', key=key).status_code == 409
    ai.fail = True
    key2 = uuid.uuid4().hex
    assert send(http, [image()], key=key2).status_code == 503
    assert send(http, [image()], key=key2).status_code == 503
    row = http.get(c.CHAT).json()['turns'][-1]
    assert row['status'] == 'failed' and row['images']
    assert http.get(row['images'][0]['url']).status_code == 200
    assert store.collection.count_documents({}) == 2 and len(ai.calls) == 2


@pytest.mark.parametrize('bad', [
    {'mime_type': 'image/png', 'data': 'not base64!!'},
    {'mime_type': 'image/png', 'data': base64.b64encode(b'<svg></svg>').decode()},
    {'mime_type': 'image/svg+xml', 'data': base64.b64encode(b'<svg></svg>').decode()},
    {'mime_type': 'image/png', 'data': 'https://foreign.example/private.png'},
    image(mime='image/jpeg'),
    image(raster(size=(4097, 1))),
])
def test_invalid_images_rejected_before_any_persistence_or_provider(photo_rig, bad):
    http, store, _, ai, _, _ = photo_rig
    assert send(http, [bad]).status_code == 422
    assert store.collection.count_documents({}) == 0 and not ai.calls


def test_corrupt_crc_count_bytes_pixels_and_body_limits(photo_rig):
    http, store, _, ai, _, _ = photo_rig
    raw = bytearray(raster());index = raw.index(b'IDAT');length = int.from_bytes(raw[index - 4:index], 'big');raw[index + 4 + length] ^= 1
    assert send(http, [image(bytes(raw))]).status_code == 422
    assert send(http, [image()] * 5).status_code == 422
    assert send(http, [image(b'x' * (c.CHAT_IMAGE_MAX_BYTES + 1))]).status_code == 422
    assert send(http, [image(raster(size=(3072, 4096)))]).status_code == 422
    assert send(http).status_code == 422
    response = http.post(c.CHAT, content=b' ' * (c.CHAT_BODY_LIMIT + 1), headers={'Content-Type': 'application/json'})
    assert response.status_code == 413
    assert store.collection.count_documents({}) == 0 and not ai.calls


@pytest.mark.parametrize('format,mime', [('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('WEBP', 'image/webp')])
def test_canonical_images_strip_metadata(photo_rig, format, mime):
    http, _, _, _, _, _ = photo_rig
    assert send(http, [image(raster(format=format, exif=True), mime)]).status_code == 200
    row = http.get(c.CHAT).json()['turns'][-1]
    saved = http.get(row['images'][0]['url']).content
    assert b'synthetic-private-metadata' not in saved
    assert not Image.open(BytesIO(saved)).getexif()


def test_failed_final_write_does_not_claim_complete_and_image_is_readable(photo_rig, monkeypatch):
    http, store, _, ai, _, _ = photo_rig
    def fail(*args, **kwargs):
        raise AutoReconnect('synthetic private mongo failure')
    with monkeypatch.context() as patch:
        patch.setattr(store.collection, 'update_one', fail)
        assert send(http, [image()]).status_code == 503
    row = http.get(c.CHAT).json()['turns'][-1]
    assert row['status'] == 'processing' and row['reply'] is None
    assert http.get(row['images'][0]['url']).status_code == 200
    assert len(ai.calls) == 1


def test_current_and_recent_images_are_bounded_and_labelled(photo_rig):
    http, store, _, ai, _, _ = photo_rig
    assert send(http, [image()] * 4, message='first four').status_code == 200
    assert send(http, message='上一張圖呢').status_code == 200
    selected = ai.calls[-1]['images']
    assert len(selected) == 4 and all('先前對話序號' in item['source'] for item in selected)
    assert sum(len(item['data']) for item in selected) <= 8 * 1024 * 1024
    assert send(http, [image(raster('blue'))] * 2, message='current first').status_code == 200
    selected = ai.calls[-1]['images']
    assert len(selected) == 4 and all('本次' in item['source'] for item in selected[:2])
    for _ in range(8):
        document, _ = store.reserve(uuid.uuid4().hex, 'later text')
        store.finish(document, response={'reply': 'later answer', 'proposal': None})
    assert send(http, message='old image beyond context').status_code == 200
    assert ai.calls[-1]['images'] == [] and len(ai.calls[-1]['history']) == 16


def test_fresh_complete_map_context_from_one_household_read(photo_rig):
    http, store, _, ai, _, mongo = photo_rig
    adapter = MongoInventory(client=mongo, database=store.collection.database.name, home_id=store.home_id)
    import_snapshot(adapter, sample())
    item = adapter.create({'name': 'synthetic item', 'category': 'tools', 'quantity': 1, 'unit': '個', 'location': '測試儲位'})
    adapter.create_location('synthetic empty location')
    furniture = {'id': 'synthetic-furniture', 'name': 'synthetic cupboard', 'kind': 'cabinet', 'room_id': 'room-one',
                 'x': 10, 'y': 20, 'width': 80, 'height': 40, 'rotation': 90, 'location_id': 97,
                 'storage': {'aspect_ratio': 1, 'compartments': [{'id': 'slot-one', 'name': 'empty slot', 'x': 0, 'y': 0, 'width': 50, 'height': 50, 'location_id': 98}]}}
    layout = sample()['layout']
    layout['rooms'] = [{'id': 'room-one', 'name': 'occupied room', 'points': [], 'label': {'x': 0, 'y': 0}}, {'id': 'empty-room', 'name': 'empty room', 'points': [], 'label': {'x': 1, 'y': 1}}]
    layout['objects'] = [furniture, dict(furniture, id='empty-furniture', room_id=None, location_id=None, storage=None)]
    adapter.collection.update_one({'_id': store.home_id}, {'$set': {'layout': layout}})
    http.app.state.grocy = adapter
    reads = []
    original = adapter.document
    def counted():
        reads.append(True)
        return original()
    adapter.document = counted
    assert send(http, message='where is everything').status_code == 200
    context = ai.calls[-1]['state']['home_context']
    assert len(reads) == 1
    assert len(context['rooms']) == len(context['furniture']) == len(context['locations']) == 2
    assert context['furniture'][0]['storage']['compartments'][0]['location_id'] == 98
    assert context['furniture'][1]['room_id'] is None
    assert context['locations'][0]['item_ids'] == [item]
    assert context['locations'][1]['item_ids'] == []
    adapter.collection.update_one({'_id': store.home_id}, {'$set': {'layout.rooms.1.name': 'fresh empty room'}, '$inc': {'version': 1}})
    assert send(http, message='fresh positions').status_code == 200
    assert ai.calls[-1]['state']['home_context']['rooms'][1]['name'] == 'fresh empty room'
    assert len(reads) == 2


def test_existing_gemini_adapter_constructs_real_typed_image_parts_without_network(monkeypatch):
    from types import SimpleNamespace
    from server import ai as module
    seen = {}
    class Client:
        def __init__(self, **kwargs):
            seen['client'] = kwargs
            self.models = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def generate_content(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(text='{"reply":"synthetic SDK answer","actions":[]}')
    monkeypatch.setattr(module.genai, 'Client', Client)
    provider = Gemini(key='synthetic-key', model='existing-configured-model')
    provider.respond('', [], {'home_context': {'rooms': []}}, '2026-10-23', images=[{'data': raster(), 'mime_type': 'image/png', 'source': '先前對話序號 1，圖片 1'}])
    assert seen['model'] == 'existing-configured-model'
    parts = seen['contents']
    assert len([part for part in parts if part.inline_data]) == 1
    assert parts[-1].inline_data.mime_type == 'image/png'
    assert parts[-1].inline_data.data == raster()
    assert '先前對話序號' in parts[-2].text
    assert 'system_instruction' in seen['config'] and '不' in seen['config']['system_instruction']
