"""Same-origin Studio bridge tests; no real layout or inventory services."""
import httpx
import pytest
from fastapi.testclient import TestClient

from server import contracts as c
from server.app import COOKIE, create_app


class NoInventory:
    def state(self):
        raise AssertionError("Studio bridge must not read inventory directly")


class NoAI:
    enabled = False


@pytest.fixture
def rig(tmp_path):
    calls = []

    def upstream(request):
        calls.append(request)
        kind = {"/": "text/html", "/app.js": "text/javascript", "/contracts.js": "application/javascript",
                "/styles.css": "text/css", c.STUDIO_REFERENCE: "image/png"}.get(request.url.path, "application/json")
        return httpx.Response(200, content=b"{}", headers={"content-type": kind,
            "set-cookie": "untrusted=upstream; Path=/", "x-frame-options": "DENY",
            "content-security-policy": "frame-ancestors *", "x-private-upstream": "do-not-relay"})

    app = create_app(NoInventory(), NoAI(), tmp_path / "test.db", "household-test", "x" * 40,
                     studio_url="http://studio.test:8780", studio_transport=httpx.MockTransport(upstream))
    client = TestClient(app, headers={"origin": "http://testserver"})
    return client, calls


def login(client):
    assert client.post(c.LOGIN, json={"password": "household-test"}).status_code == 200


@pytest.mark.parametrize("method,path", [("GET", c.MAP_STUDIO), ("GET", c.MAP_STUDIO + "app.js"),
    ("GET", c.STUDIO_LAYOUT), ("PUT", c.STUDIO_LAYOUT), ("GET", c.STUDIO_REFERENCE),
    ("GET", c.STUDIO_EXPORT), ("GET", c.LOCATIONS), ("DELETE", c.ITEMS + "/1"),
    ("POST", c.STUDIO_CLEAR.format(object_id="cabinet_1"))])
def test_every_studio_route_requires_original_session(rig, method, path):
    client, calls = rig
    assert client.request(method, path, json={} if method != "GET" else None).status_code == 401
    assert calls == []


@pytest.mark.parametrize("path,upstream_path,mime", [(c.MAP_STUDIO + "?embedded=1", "/", "text/html"),
    (c.MAP_STUDIO + "app.js", "/app.js", "application/javascript"),
    (c.MAP_STUDIO + "contracts.js", "/contracts.js", "application/javascript"),
    (c.MAP_STUDIO + "styles.css", "/styles.css", "text/css"),
    (c.STUDIO_LAYOUT, c.STUDIO_LAYOUT, "application/json"),
    (c.STUDIO_REFERENCE, c.STUDIO_REFERENCE, "image/png"),
    (c.STUDIO_EXPORT, c.STUDIO_EXPORT, "application/json"), (c.LOCATIONS, c.LOCATIONS, "application/json")])
def test_fixed_routes_control_headers_and_framing(rig, path, upstream_path, mime):
    client, calls = rig
    login(client)
    token = client.cookies[COOKIE]
    client.cookies.set("other_private_cookie", "must-not-forward")
    response = client.get(path, headers={"origin": "https://evil.test", "authorization": "private-value",
        "x-forwarded-host": "evil.test", "x-forwarded-proto": "https", "grocy-api-key": "private-value"})
    assert response.status_code == 200
    sent = calls[-1]
    assert sent.url == "http://studio.test:8780" + upstream_path
    assert sent.headers["origin"] == "http://studio.test:8780"
    assert sent.headers["cookie"] == f"{COOKIE}={token}"
    for header in ("authorization", "grocy-api-key", "x-forwarded-host", "x-forwarded-proto"):
        assert header not in sent.headers
    assert response.headers["content-type"].split(";", 1)[0] == mime
    assert "set-cookie" not in response.headers and "x-private-upstream" not in response.headers
    assert response.headers["x-frame-options"] == ("SAMEORIGIN" if path.startswith(c.MAP_STUDIO) else "DENY")
    expected = "'self'" if path.startswith(c.MAP_STUDIO) else "'none'"
    assert f"frame-ancestors {expected}" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == ("no-cache" if path.startswith(c.MAP_STUDIO) else "no-store")
    if path == c.STUDIO_EXPORT:
        assert response.headers["content-disposition"] == 'attachment; filename="home-map.json"'


def test_parent_only_permits_same_origin_children(rig):
    client, _ = rig
    response = client.get("/")
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "frame-src 'self'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("path", [c.MAP_STUDIO + "server.py", c.MAP_STUDIO + ".env",
    c.MAP_STUDIO + "http://evil.test", c.MAP_STUDIO + "%2e%2e/server.py",
    "/api/studio/proxy/http://evil.test", c.STUDIO_LAYOUT + "/extra"])
def test_unlisted_paths_never_proxy(rig, path):
    client, calls = rig
    login(client)
    assert client.get(path).status_code in {404, 405}
    assert calls == []


@pytest.mark.parametrize("method,path", [("PUT", c.STUDIO_LAYOUT), ("DELETE", c.ITEMS + "/1"),
    ("POST", c.STUDIO_CLEAR.format(object_id="cabinet_1"))])
def test_studio_writes_keep_original_csrf_json_guards(rig, method, path):
    client, calls = rig
    login(client)
    for headers, expected in [({"origin": "https://evil.test"}, 403),
                              ({"sec-fetch-site": "same-site"}, 403),
                              ({"sec-fetch-site": "cross-site"}, 403),
                              ({"content-type": "text/plain"}, 415)]:
        assert client.request(method, path, content="{}", headers={"content-type": "application/json", **headers}).status_code == expected
    client.headers.pop("origin")
    assert client.request(method, path, json={}).status_code == 403
    assert calls == []


def test_stream_bytes_and_separate_layout_limit(rig):
    client, calls = rig
    login(client)
    payload = b'"' + b"x" * (c.STUDIO_BODY_LIMIT - 2) + b'"'
    assert client.put(c.STUDIO_LAYOUT, content=payload, headers={"content-type": "application/json"}).status_code == 200
    assert len(calls[-1].content) == c.STUDIO_BODY_LIMIT
    calls.clear()
    for length in (str(c.STUDIO_BODY_LIMIT + 1), "0", "not-a-length"):
        result = client.put(c.STUDIO_LAYOUT, content=payload + b"x", headers={
            "content-type": "application/json", "content-length": length})
        assert result.status_code == 413
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["x-frame-options"] == "DENY"
    response = client.request("DELETE", c.ITEMS + "/1", content=b"x" * 100001,
                              headers={"content-type": "application/json"})
    assert response.status_code == 413 and calls == []


@pytest.mark.parametrize("path", [c.ITEMS + "/0", c.ITEMS + "/-1", c.ITEMS + "/01", c.ITEMS + "/1.0",
    c.STUDIO_CLEAR.format(object_id="unsafe%3Fid"), c.STUDIO_CLEAR.format(object_id="x" * 81)])
def test_dynamic_ids_validated_before_forwarding(rig, path):
    client, calls = rig
    login(client)
    method = "POST" if path.endswith("/clear") else "DELETE"
    assert client.request(method, path, json={}).status_code == 422
    assert calls == []


def test_valid_writes_keep_json_and_idempotency_but_drop_query(rig):
    client, calls = rig
    login(client)
    for method, path in [("PUT", c.STUDIO_LAYOUT), ("DELETE", c.ITEMS + "/42"),
                         ("POST", c.STUDIO_CLEAR.format(object_id="cabinet-1"))]:
        result = client.request(method, path + "?url=http://evil.test", json={"synthetic": True},
                                headers={"idempotency-key": "request-studio-123"})
        assert result.status_code == 200
        assert calls[-1].url.path == path and not calls[-1].url.query
        assert calls[-1].headers["content-type"] == "application/json"
        assert calls[-1].headers["idempotency-key"] == "request-studio-123"
    assert client.post(c.LOGOUT, json={}).status_code == 200
    count = len(calls)
    assert client.get(c.STUDIO_LAYOUT).status_code == 401 and len(calls) == count


@pytest.mark.parametrize("mode", ["network", "redirect", "server-error", "wrong-type"])
def test_upstream_failures_are_generic(tmp_path, mode):
    calls = []

    def upstream(request):
        calls.append(request)
        if mode == "network":
            raise httpx.ConnectError("private upstream connection details", request=request)
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.test/private"})
        if mode == "server-error":
            return httpx.Response(500, json={"detail": "private upstream server details"})
        return httpx.Response(200, text="private upstream markup")

    app = create_app(NoInventory(), NoAI(), tmp_path / "test.db", "household-test", "x" * 40,
                     studio_transport=httpx.MockTransport(upstream))
    client = TestClient(app, headers={"origin": "http://testserver"})
    login(client)
    response = client.get(c.STUDIO_LAYOUT)
    assert response.status_code == 502 and len(calls) == 1
    assert "private" not in response.text and "evil" not in response.text
    assert "location" not in response.headers and response.headers["cache-control"] == "no-store"


def test_conflicts_remain_actionable_without_upstream_headers(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(409, json={"detail": "synthetic revision conflict"},
                                  headers={"set-cookie": "other=invalid", "location": "https://evil.test"}))
    app = create_app(NoInventory(), NoAI(), tmp_path / "test.db", "household-test", "x" * 40,
                     studio_transport=transport)
    client = TestClient(app, headers={"origin": "http://testserver"})
    login(client)
    response = client.put(c.STUDIO_LAYOUT, json={})
    assert response.status_code == 409 and response.json()["detail"] == "synthetic revision conflict"
    assert "set-cookie" not in response.headers and "location" not in response.headers


@pytest.mark.parametrize("url", ["https://evil.test/path", "http://user:password@studio.test", "http://studio.test?url=evil", "file:///tmp/studio"])
def test_configuration_only_accepts_bare_trusted_origin(tmp_path, url):
    with pytest.raises(ValueError, match="bare trusted HTTP origin"):
        create_app(NoInventory(), NoAI(), tmp_path / "test.db", "household-test", "x" * 40, studio_url=url)


def test_studio_can_call_parent_session_during_proxy_without_lock(tmp_path):
    callback_results = []
    app = None

    async def upstream(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as parent:
            response = await parent.get(c.SESSION, headers={"cookie": request.headers["cookie"]})
        callback_results.append(response.json()["authenticated"])
        return httpx.Response(200, json={"synthetic": True})

    app = create_app(NoInventory(), NoAI(), tmp_path / "test.db", "household-test", "x" * 40,
                     studio_transport=httpx.MockTransport(upstream))
    client = TestClient(app, headers={"origin": "http://testserver"})
    login(client)
    assert client.get(c.STUDIO_LAYOUT).status_code == 200
    assert callback_results == [True]
