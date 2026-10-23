"""Independent synthetic checks of the fixed model and local vendor bridge."""
import httpx
import pytest
from fastapi.testclient import TestClient

from server import contracts as c
from server.app import COOKIE, create_app


class NoHouseholdReads:
    def state(self):
        raise AssertionError("Model bridge must not read household content")


@pytest.fixture
def bridge(tmp_path):
    calls = []
    mode = {"status": 200, "mime": "model/gltf-binary", "body": b"glTFsynthetic-binary\x00\xff"}
    def upstream(request):
        calls.append(request)
        return httpx.Response(mode["status"], content=mode["body"], headers={"content-type": mode["mime"],
            "set-cookie": "untrusted=private; Path=/", "location": "https://evil.invalid/private", "x-private": "never-relay"})
    app = create_app(NoHouseholdReads(), object(), tmp_path / "synthetic.sqlite3", "test-only", "q" * 40,
                     studio_url="http://studio.test:8780", studio_transport=httpx.MockTransport(upstream))
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        yield client, calls, mode


def login(client):
    assert client.post(c.LOGIN, json={"password": "test-only"}).status_code == 200


@pytest.mark.parametrize("path", [c.STUDIO_MODELS, c.STUDIO_MODEL.format(object_id="qa-cabinet"),
                                   c.MAP_STUDIO + "model-viewer.js", c.MAP_STUDIO + "vendor/three.module.js"])
def test_independent_private_model_bridge_requires_parent_session(bridge, path):
    client, calls, _ = bridge
    assert client.get(path).status_code == 401
    assert calls == []


def test_independent_model_proxy_preserves_binary_mime_and_quarantines_headers(bridge):
    client, calls, mode = bridge
    login(client)
    client.cookies.set("unrelated", "must-not-forward")
    path = c.STUDIO_MODEL.format(object_id="qa-cabinet")
    response = client.get(path + "?url=https://evil.invalid/x", headers={"authorization": "never-forward"})
    assert response.status_code == 200 and response.content == mode["body"]
    assert response.headers["content-type"].split(";", 1)[0] == "model/gltf-binary"
    assert response.headers["cache-control"] == "no-store"
    assert not {"set-cookie", "location", "x-private"} & response.headers.keys()
    sent = calls[-1]
    assert str(sent.url) == "http://studio.test:8780" + path
    assert "authorization" not in sent.headers
    assert sent.headers["cookie"] == f"{COOKIE}={client.cookies.get(COOKIE)}"


@pytest.mark.parametrize("status,mime", [(200, "image/svg+xml"), (200, "text/html"),
                                         (200, "application/json"), (302, "model/gltf-binary")])
def test_independent_binary_proxy_rejects_wrong_mime_and_redirects(bridge, status, mime):
    client, calls, mode = bridge
    login(client)
    mode.update(status=status, mime=mime, body=b"private-upstream-sentinel")
    response = client.get(c.STUDIO_MODEL.format(object_id="qa-cabinet"))
    assert response.status_code == 502 and len(calls) == 1
    assert "private-upstream-sentinel" not in response.text and "location" not in response.headers


@pytest.mark.parametrize("suffix", ["bad.id", "bad%3Fid", "x" * 81, "safe/extra", "%2e%2e%2f.env"])
def test_independent_model_proxy_never_forwards_invalid_ids(bridge, suffix):
    client, calls, _ = bridge
    login(client)
    response = client.get(c.STUDIO_MODEL.format(object_id=suffix))
    assert response.status_code in {404, 422} and not calls


@pytest.mark.parametrize("asset", ["model-viewer.js", "vendor/three.module.js", "vendor/three.core.js",
    "vendor/OrbitControls.js", "vendor/GLTFLoader.js", "vendor/BufferGeometryUtils.js", "vendor/SkeletonUtils.js", "vendor/RoomEnvironment.js"])
def test_independent_vendor_assets_are_fixed_local_javascript_paths(bridge, asset):
    client, calls, mode = bridge
    login(client)
    mode.update(mime="text/javascript", body=b"// synthetic javascript")
    response = client.get(c.MAP_STUDIO + asset + "?url=https://evil.invalid/script.js")
    assert response.status_code == 200
    assert str(calls[-1].url) == "http://studio.test:8780/" + asset
    assert response.headers["content-type"].split(";", 1)[0] == "application/javascript"


@pytest.mark.parametrize("asset", ["vendor/.env", "vendor/server.py", "vendor/arbitrary.js",
                                   "vendor/GLTFLoader.js/extra", "vendor/http://evil.invalid/x.js"])
def test_independent_vendor_bridge_cannot_become_arbitrary_proxy(bridge, asset):
    client, calls, _ = bridge
    login(client)
    assert client.get(c.MAP_STUDIO + asset).status_code == 404
    assert not calls


def test_independent_blob_texture_permission_is_limited_to_map_frame(bridge):
    client, _, mode = bridge
    login(client)
    mode.update(mime="text/html", body=b"<!doctype html><title>Synthetic</title>")
    def directives(response):
        return {part.strip().split()[0]: part.strip().split()[1:]
                for part in response.headers["content-security-policy"].split(";") if part.strip()}
    embedded = directives(client.get(c.MAP_STUDIO))
    parent = directives(client.get("/"))
    assert set(embedded["connect-src"]) == {"'self'", "blob:"}
    assert parent["connect-src"] == ["'self'"]
    assert "*" not in embedded["connect-src"]
    assert not any(source.startswith(("http:", "https:")) for source in embedded["connect-src"])
