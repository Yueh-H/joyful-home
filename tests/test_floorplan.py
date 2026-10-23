"""Synthetic-only map tests; no real household image, layout or Grocy writes."""
import base64
import copy
from io import BytesIO
import json
import struct
import zlib

from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
import pytest
from server.app import create_app
from server import contracts as c
from server.grocy import Grocy


class MapGrocy:
    def __init__(self):
        self.rows = [{"id": 1, "name": "測試架"}, {"id": 2, "name": "測試箱"}]
        self.products = [{"id": 1, "name": "測試物品", "location_id": 1, "quantity": 3}]
        self.writes = []

    def locations(self):
        return copy.deepcopy(self.rows)

    def state(self):
        return {"items": [{**item, "location": next(row["name"] for row in self.rows if row["id"] == item["location_id"])} for item in self.products], "shopping": []}

    def create_location(self, name):
        result = {"id": len(self.rows) + 1, "name": name}
        self.rows.append(result)
        self.writes.append(("create_location", name))
        return result

    def rename_location(self, location_id, name):
        next(row for row in self.rows if row["id"] == location_id)["name"] = name
        self.writes.append(("rename_location", location_id, name))
        return {"id": location_id, "name": name}

    def patch(self, pid, patch, current):
        self.writes.append(("patch_item", pid, patch))
        item = next(row for row in self.products if row["id"] == pid)
        item["location_id"] = next(row["id"] for row in self.rows if row["name"] == patch["location"])


class InspectAI:
    enabled = True
    seen = None

    def respond(self, message, history, state, today):
        self.seen = copy.deepcopy(state)
        return c.AIResponse(reply="尚未變更")


def plan(**changes):
    return {"revision": 0, "title": "Synthetic QA", "canvas": {"width": 1000, "height": 800},
            "rooms": [{"id": "room-1", "name": "測試區域", "color": "#456789", "points": [{"x": .1, "y": .1}, {"x": .8, "y": .1}, {"x": .8, "y": .8}]}],
            "spots": [{"id": "spot-1", "location_id": 1, "room_id": "room-1", "x": .4, "y": .3}], **changes}


def image_url(fmt="PNG", size=(400, 300), metadata=False):
    output = BytesIO()
    kwargs = {}
    if metadata:
        info = PngImagePlugin.PngInfo()
        info.add_text("private", "metadata-must-disappear")
        kwargs["pnginfo"] = info
    Image.new("RGB", size, "white").save(output, format=fmt, **kwargs)
    mime = {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp", "GIF": "gif"}[fmt]
    return f"data:image/{mime};base64," + base64.b64encode(output.getvalue()).decode()


@pytest.fixture
def rig(tmp_path):
    grocy, ai = MapGrocy(), InspectAI()
    db_path = tmp_path / "runtime.db"
    app = create_app(grocy, ai, db_path, "test-only", "s" * 40)
    client = TestClient(app, headers={"Origin": "http://testserver"})
    return client, grocy, ai, db_path


def login(client):
    assert client.post(c.LOGIN, json={"password": "test-only"}).status_code == 200


def test_map_auth_csrf_and_no_store(rig):
    client, grocy, _, _ = rig
    for route in [c.FLOORPLAN, c.FLOORPLAN_IMAGE]:
        result = client.get(route)
        assert result.status_code == 401 and result.headers["cache-control"] == "no-store"
    assert client.put(c.FLOORPLAN, json=plan()).status_code == 401
    assert client.post(c.LOCATIONS, json={"name": "x"}).status_code == 401
    assert client.patch(c.LOCATIONS + "/1", json={"name": "x"}).status_code == 401
    login(client)
    for method, route, body in [("PUT", c.FLOORPLAN, plan()), ("POST", c.LOCATIONS, {"name": "x"}), ("PATCH", c.LOCATIONS + "/1", {"name": "x"})]:
        assert client.request(method, route, json=body, headers={"origin": "https://evil.invalid"}).status_code == 403
    result = client.get(c.FLOORPLAN)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json() == {"revision": 0, "title": "家的地圖", "canvas": {"width": 1000, "height": 800}, "rooms": [], "spots": [], "image_url": None, "locations": grocy.rows}
    assert not grocy.writes


def test_revision_persistence_and_export_privacy(rig):
    client, grocy, ai, db_path = rig
    login(client)
    result = client.put(c.FLOORPLAN, json=plan())
    assert result.status_code == 200 and result.json()["revision"] == 1
    assert client.put(c.FLOORPLAN, json=plan(title="stale")).status_code == 409
    assert client.get(c.FLOORPLAN).json()["title"] == "Synthetic QA"
    exported = client.get(c.EXPORT)
    assert exported.headers["cache-control"] == "no-store"
    assert exported.json()["floorplan"]["rooms"] == plan()["rooms"]
    assert "floorplan" not in client.get(c.STATE).json()
    assert client.post(c.CHAT, json={"message": "目前庫存"}).status_code == 200
    assert "floorplan" not in ai.seen and "points" not in json.dumps(ai.seen)
    restarted = TestClient(create_app(grocy, ai, db_path, "test-only", "s" * 40), headers={"Origin": "http://testserver"})
    login(restarted)
    assert restarted.get(c.FLOORPLAN).json() == result.json()
    assert not grocy.writes


@pytest.mark.parametrize("mutate", [
    lambda d: d["rooms"][0]["points"][0].update(x=1.01),
    lambda d: d["spots"][0].update(y=-.1),
    lambda d: d["spots"][0].update(x="NaN"),
    lambda d: d["spots"][0].update(location_id=999),
    lambda d: d["spots"][0].update(room_id="absent"),
    lambda d: d["spots"].append({**d["spots"][0], "id": "other"}),
    lambda d: d["rooms"].append(copy.deepcopy(d["rooms"][0])),
    lambda d: d["rooms"][0].update(id="../escape"),
    lambda d: d["rooms"][0].update(color="url(https://evil.invalid)"),
    lambda d: d["rooms"][0].update(points=[{"x": .1, "y": .1}] * 3),
    lambda d: d["canvas"].update(width=8000, height=8000),
    lambda d: d.update(image_url="file:///tmp/private.png"),
    lambda d: d.update(image_data_url="https://evil.invalid/image.png"),
    lambda d: d.update(rooms=d["rooms"] * 61),
    lambda d: d.update(spots=d["spots"] * 301),
])
def test_invalid_geometry_never_writes(rig, mutate):
    client, grocy, _, _ = rig
    login(client)
    body = plan()
    mutate(body)
    assert client.put(c.FLOORPLAN, json=body).status_code == 422
    assert client.get(c.FLOORPLAN).json()["revision"] == 0
    assert not grocy.writes


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_private_raster_metadata_dimensions_lifecycle(rig, fmt):
    client, grocy, ai, db_path = rig
    login(client)
    saved = client.put(c.FLOORPLAN, json=plan(image_data_url=image_url(fmt, metadata=fmt == "PNG"))).json()
    assert saved["revision"] == 1 and saved["canvas"] == {"width": 400, "height": 300}
    response = client.get(saved["image_url"])
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store" and response.headers["content-type"] == "image/png"
    with Image.open(BytesIO(response.content)) as decoded:
        assert decoded.size == (400, 300) and not decoded.info
    assert b"metadata-must-disappear" not in response.content
    assert len(list((db_path.parent / "floorplans").glob("*.png"))) == 1
    restarted = TestClient(create_app(grocy, ai, db_path, "test-only", "s" * 40), headers={"Origin": "http://testserver"})
    assert restarted.get(c.FLOORPLAN_IMAGE).status_code == 401
    login(restarted)
    assert restarted.get(c.FLOORPLAN_IMAGE).content == response.content
    assert client.put(c.FLOORPLAN, json=plan(revision=1)).status_code == 200
    assert client.get(c.FLOORPLAN_IMAGE).content == response.content
    assert client.put(c.FLOORPLAN, json=plan(revision=2, image_data_url=None)).status_code == 200
    assert client.get(c.FLOORPLAN_IMAGE).status_code == 404
    assert not list((db_path.parent / "floorplans").glob("*.png"))
    assert not grocy.writes


@pytest.mark.parametrize("value", ["data:image/png;base64,not-valid=", "data:image/png;base64,PHN2Zz48L3N2Zz4=", image_url("GIF"), image_url(size=(50, 50)), image_url("JPEG").replace("image/jpeg", "image/png")])
def test_invalid_images_rejected(rig, value):
    client, _, _, _ = rig
    login(client)
    assert client.put(c.FLOORPLAN, json=plan(image_data_url=value)).status_code == 422
    assert client.get(c.FLOORPLAN_IMAGE).status_code == 404


def test_image_and_request_limits(rig):
    client, _, _, _ = rig
    login(client)
    # A synthetic PNG header advertises a huge raster; no huge allocation is made.
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    bomb = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 5000, 5000, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")
    assert client.put(c.FLOORPLAN, json=plan(image_data_url="data:image/png;base64," + base64.b64encode(bomb).decode())).status_code == 422
    huge = "data:image/png;base64," + base64.b64encode(b"x" * (10 * 1024 * 1024 + 1)).decode()
    assert client.put(c.FLOORPLAN, json=plan(image_data_url=huge)).status_code == 422
    # Only map PUT receives the larger limit, and actual bytes enforce it too.
    assert client.put(c.FLOORPLAN, content=b" " * 100001).status_code == 415
    headers = {"Content-Type": "application/json"}
    large_valid = json.dumps(plan()).encode() + b" " * 100001
    assert client.put(c.FLOORPLAN, content=large_valid, headers=headers).status_code == 200
    assert client.post(c.LOCATIONS, content=b" " * 100001, headers=headers).status_code == 413
    assert client.put(c.FLOORPLAN, content=b"x" * (c.FLOORPLAN_BODY_LIMIT + 1), headers={**headers, "Content-Length": "1"}).status_code == 413


def test_canonical_location_rename_unpin_and_inventory_assignment(rig):
    client, grocy, _, _ = rig
    login(client)
    assert client.post(c.LOCATIONS, json={"name": "測試架"}).json() == {"id": 1, "name": "測試架"}
    assert not grocy.writes
    assert client.post(c.LOCATIONS, json={"name": "新測試架"}).json() == {"id": 3, "name": "新測試架"}
    assert client.patch(c.LOCATIONS + "/1", json={"name": "測試箱"}).status_code == 409
    assert client.patch(c.LOCATIONS + "/999", json={"name": "不存在"}).status_code == 404
    assert client.patch(c.LOCATIONS + "/1", json={"name": "改名測試架"}).json() == {"id": 1, "name": "改名測試架"}
    assert client.get(c.STATE).json()["items"][0]["location"] == "改名測試架"
    assert client.put(c.FLOORPLAN, json=plan()).status_code == 200
    # Removing a room preserves its pin when the draft sends room_id=null.
    assert client.put(c.FLOORPLAN, json=plan(revision=1, rooms=[], spots=[{**plan()["spots"][0], "room_id": None}])).status_code == 200
    assert client.put(c.FLOORPLAN, json=plan(revision=2, rooms=[], spots=[])).status_code == 200
    assert len(grocy.products) == 1 and grocy.products[0]["quantity"] == 3
    assert client.patch(c.ITEMS + "/1", json={"location": "測試箱"}).status_code == 200
    assert client.get(c.STATE).json()["items"][0]["location_id"] == 2
    assert len(grocy.products) == 1 and grocy.products[0]["quantity"] == 3
    assert not any("DELETE" in str(write) for write in grocy.writes)


def test_real_adapter_location_routes_and_normalized_ids():
    grocy = Grocy("http://unused", "test-only")
    calls = []
    entities = {"products": [{"id": "1", "name": "測試", "location_id": "2", "qu_id_stock": "1"}], "locations": [{"id": "2", "name": "測試位置"}], "quantity_units": [], "product_groups": [], "stock": [], "shopping_list": []}
    grocy.objects = lambda entity: entities[entity]
    def request(method, path, data=None):
        calls.append((method, path, data))
        return {"created_object_id": "3"}
    grocy.request = request
    assert grocy.state()["items"][0]["location_id"] == 2
    assert grocy.create_location("測試位置") == {"id": 2, "name": "測試位置"} and not calls
    assert grocy.create_location("新位置")["id"] == 3
    assert grocy.rename_location(2, "新名稱") == {"id": 2, "name": "新名稱"}
    assert calls == [("POST", "objects/locations", {"name": "新位置"}), ("PUT", "objects/locations/2", {"name": "新名稱"})]


@pytest.mark.parametrize("route", ["/", "/app.js", "/contracts.js", "/floorplan.js", "/sw.js"])
def test_static_ui_revalidates_on_upgrade_and_api_stays_private(rig, route):
    client, _, _, _ = rig
    response = client.get(route)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    # Both unauthenticated and authenticated private responses must never cache.
    assert client.get(c.FLOORPLAN).headers["cache-control"] == "no-store"
    login(client)
    assert client.get(c.FLOORPLAN).headers["cache-control"] == "no-store"
