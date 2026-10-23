"""Independent integration boundary checks; synthetic transports and temporary DB only."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from server.app import COOKIE, create_app
from server import contracts as c


class ReadOnlyHousehold:
    def state(self):
        return {"items": [], "shopping": []}

    def locations(self):
        return []


@pytest.fixture
def boundary(tmp_path):
    calls = []
    mode = {"status": 200, "mime": "application/json", "body": '{"qa":true}'}

    def upstream(request):
        calls.append(request)
        mime = mode["mime"]
        body = mode["body"]
        if request.url.path == "/" and mode["status"] == 200:
            mime, body = "text/html", "<!doctype html><title>Synthetic Studio</title>"
        return httpx.Response(mode["status"], content=body, headers={
            "content-type": mime, "set-cookie": "intruder=private; Path=/",
            "location": "https://evil.invalid/private", "x-frame-options": "ALLOWALL",
            "content-security-policy": "default-src *", "x-private": "never-relay"})

    app = create_app(ReadOnlyHousehold(), object(), tmp_path / "qa.sqlite3", "synthetic-only", "q" * 40,
                     studio_url="http://studio.test:8780", studio_transport=httpx.MockTransport(upstream))
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        yield client, calls, mode


def authenticate(client):
    assert client.post(c.LOGIN, json={"password": "synthetic-only"}).status_code == 200


@pytest.mark.parametrize("path", [c.MAP_STUDIO, c.MAP_STUDIO + "app.js", c.STUDIO_LAYOUT,
                                   c.STUDIO_REFERENCE, c.STUDIO_EXPORT, c.LOCATIONS])
def test_independent_studio_never_calls_upstream_without_session(boundary, path):
    client, calls, _ = boundary
    result = client.get(path)
    assert result.status_code == 401
    assert not calls
    if path.startswith(c.API):
        assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("suffix", [".env", "server.py", "../.env", "%2e%2e%2f.env", "https://evil.invalid/x", "app.js/extra"])
def test_independent_static_allowlist_never_becomes_url_proxy(boundary, suffix):
    client, calls, _ = boundary
    authenticate(client)
    assert client.get(c.MAP_STUDIO + suffix).status_code == 404
    assert not calls


def test_independent_browser_frame_guards_and_header_quarantine(boundary):
    client, calls, _ = boundary
    authenticate(client)
    parent = client.get("/")
    child = client.get(c.MAP_STUDIO + "?url=https://evil.invalid/private")
    assert parent.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in parent.headers["content-security-policy"]
    assert "frame-src 'self'" in parent.headers["content-security-policy"]
    assert child.status_code == 200
    assert child.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in child.headers["content-security-policy"]
    assert "set-cookie" not in child.headers and "x-private" not in child.headers and "location" not in child.headers
    assert str(calls[-1].url) == "http://studio.test:8780/"
    assert client.cookies.get("intruder") is None


def test_independent_forward_headers_are_controlled_and_query_is_dropped(boundary):
    client, calls, _ = boundary
    authenticate(client)
    result = client.get(c.STUDIO_LAYOUT + "?upstream=https://evil.invalid/", headers={
        "authorization": "Bearer never-forward", "x-grocy-api-key": "never-forward",
        "cookie": f"{COOKIE}={client.cookies.get(COOKIE)}; extra=never-forward",
        "origin": "https://evil.invalid", "idempotency-key": "qa-key"})
    assert result.status_code == 200
    request = calls[-1]
    assert str(request.url) == "http://studio.test:8780" + c.STUDIO_LAYOUT
    assert request.headers["origin"] == "http://studio.test:8780"
    assert request.headers["cookie"].startswith(COOKIE + "=") and ";" not in request.headers["cookie"]
    assert request.headers["idempotency-key"] == "qa-key"
    assert "authorization" not in request.headers and "x-grocy-api-key" not in request.headers
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("headers,status", [({"Origin": "null"},403), ({"Origin": "http://testserver", "sec-fetch-site": "same-site"},403),
    ({"Origin": "http://testserver", "content-type": "text/plain"},415), ({"Origin": "http://testserver", "content-length": str(c.STUDIO_BODY_LIMIT + 1)},413)])
def test_independent_mutation_guards_precede_upstream(boundary, headers, status):
    client, calls, _ = boundary
    authenticate(client)
    result = client.put(c.STUDIO_LAYOUT, content=b'{}', headers={"content-type": "application/json", **headers})
    assert result.status_code == status
    assert not calls


def test_independent_forged_length_cannot_bypass_layout_cap(boundary):
    client, calls, _ = boundary
    authenticate(client)
    result = client.put(c.STUDIO_LAYOUT, content=b"x" * (c.STUDIO_BODY_LIMIT + 1),
                        headers={"content-type": "application/json", "content-length": "1"})
    assert result.status_code == 413 and not calls


@pytest.mark.parametrize("status,mime", [(302,"application/json"),(500,"application/json"),(200,"text/html")])
def test_independent_redirect_server_error_and_wrong_mime_are_quarantined(boundary, status, mime):
    client, calls, mode = boundary
    authenticate(client)
    mode.update(status=status, mime=mime, body="private-stacktrace-secret")
    result = client.get(c.STUDIO_LAYOUT)
    assert result.status_code == 502 and "private-stacktrace-secret" not in result.text
    assert "location" not in result.headers and len(calls) == 1


@pytest.mark.parametrize("path,method", [(c.STUDIO_CLEAR.format(object_id="bad.id"),"POST"),(c.ITEMS + "/0","DELETE"),
    (c.ITEMS + "/01","DELETE"),(c.ITEMS + "/1e2","DELETE"),(c.STUDIO_LAYOUT + "/extra","PUT")])
def test_independent_unknown_ids_and_api_suffixes_are_never_forwarded(boundary, path, method):
    client, calls, _ = boundary
    authenticate(client)
    assert client.request(method,path,json={}).status_code in {404,405,422}
    assert not calls


def test_independent_logout_revokes_child_and_parent_private_reads(boundary):
    client, calls, _ = boundary
    authenticate(client)
    token = client.cookies.get(COOKIE)
    assert client.post(c.LOGOUT,json={}).status_code == 200
    client.cookies.set(COOKIE,token)
    for path in (c.MAP_STUDIO,c.STUDIO_LAYOUT,c.STUDIO_EXPORT,c.FLOORPLAN_IMAGE,c.STATE):
        assert client.get(path).status_code == 401
    assert not calls


@pytest.mark.parametrize("url", ["file:///etc/passwd","http://name:secret@studio.test","http://studio.test/path","http://studio.test?url=evil","http://studio.test#fragment"])
def test_independent_invalid_config_cannot_inject_an_upstream_url(tmp_path,url):
    with pytest.raises(ValueError,match="bare trusted HTTP origin"):
        create_app(ReadOnlyHousehold(),object(),tmp_path / "invalid.sqlite3","qa","q" * 40,studio_url=url)


@pytest.mark.parametrize("path,method", [(c.STUDIO_LAYOUT,"PUT"),
    (c.STUDIO_CLEAR.format(object_id="safe_qa-id"),"POST"),(c.ITEMS + "/101","DELETE")])
def test_independent_write_routes_require_live_parent_session(boundary,path,method):
    client,calls,_ = boundary
    assert client.request(method,path,json={}).status_code == 401
    assert not calls


def test_independent_layout_uses_two_megabyte_limit_without_raising_other_native_api_caps(boundary):
    client,calls,_ = boundary
    authenticate(client)
    body = b'{"synthetic_padding":"' + b'x' * 110000 + b'"}'
    assert client.put(c.STUDIO_LAYOUT,content=body,headers={"content-type":"application/json"}).status_code == 200
    assert len(calls) == 1 and calls[0].content == body
    calls.clear()
    assert client.post(c.ACTIONS,content=body,headers={"content-type":"application/json"}).status_code == 413
    # Chat alone now accepts bounded image payloads; malformed fields still fail validation.
    assert client.post(c.CHAT,content=body,headers={"content-type":"application/json"}).status_code == 422
    assert not calls


def test_independent_legacy_and_studio_delete_share_route_without_guard_bypass(boundary):
    client,calls,_ = boundary
    authenticate(client)
    # Native empty-body deletion resolves the native inventory and cannot become a proxy delete.
    assert client.request("DELETE",c.ITEMS + "/101",json={}).status_code == 404
    assert not calls
    confirmation = {"confirm_name":"Synthetic only","expected_location_id":7}
    result = client.request("DELETE",c.ITEMS + "/101",json=confirmation)
    assert result.status_code == 200 and len(calls) == 1
    assert calls[0].url.path == c.ITEMS + "/101"
    assert json.loads(calls[0].content) == confirmation
    calls.clear()
    assert client.request("DELETE",c.ITEMS + "/101",content=b"{broken",headers={"content-type":"application/json"}).status_code == 422
    assert not calls
