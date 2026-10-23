"""Independent adversarial checks using synthetic data and no external services."""
import base64
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import socket
import sys
import threading
import time

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest
import uvicorn

from server.app import COOKIE, create_app
from server import contracts as c


class ReadOnlyGrocy:
    def locations(self):
        return [{"id": 1, "name": "Independent synthetic shelf"}]


def synthetic_plan(revision=0, **changes):
    return {"revision": revision, "title": "Independent synthetic plan",
            "canvas": {"width": 400, "height": 300}, "rooms": [], "spots": [], **changes}


def synthetic_image(color):
    output = BytesIO()
    Image.new("RGB", (400, 300), color).save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


@pytest.fixture
def clients(tmp_path):
    path = tmp_path / "independent.sqlite3"
    # Separate app instances also have separate locks: SQLite must arbitrate.
    apps = [create_app(ReadOnlyGrocy(), object(), path, "qa-only", "q" * 40) for _ in range(2)]
    result = [TestClient(app, headers={"Origin": "http://testserver"}) for app in apps]
    for client in result:
        assert client.post(c.LOGIN, json={"password": "qa-only"}).status_code == 200
    yield result, path
    for client in result:
        client.close()


def test_independent_concurrent_device_save_has_one_winner(clients):
    devices, _ = clients
    barrier = threading.Barrier(2)

    def submit(index):
        barrier.wait(timeout=5)
        return devices[index].put(c.FLOORPLAN, json=synthetic_plan(title=f"Device {index}"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json() for response in responses if response.status_code == 200)
    assert devices[0].get(c.FLOORPLAN).json() == winner
    assert devices[1].get(c.FLOORPLAN).json() == winner


def test_independent_failed_image_updates_keep_current_pixels(clients):
    (client, other), path = clients
    saved = client.put(c.FLOORPLAN, json=synthetic_plan(image_data_url=synthetic_image("white")))
    assert saved.status_code == 200
    pixels = client.get(c.FLOORPLAN_IMAGE).content
    files = set((path.parent / "floorplans").glob("*.png"))
    assert len(files) == 1
    assert other.put(c.FLOORPLAN, json=synthetic_plan(image_data_url=synthetic_image("black"))).status_code == 409
    assert client.put(c.FLOORPLAN, json=synthetic_plan(1, image_data_url="data:image/png;base64,AAAA")).status_code == 422
    assert set((path.parent / "floorplans").glob("*.png")) == files
    assert client.get(c.FLOORPLAN_IMAGE).content == pixels
    assert client.get(c.FLOORPLAN).json()["revision"] == 1


def test_independent_logout_revokes_replayed_cookie_and_images(clients):
    (client, other), _ = clients
    assert client.put(c.FLOORPLAN, json=synthetic_plan(image_data_url=synthetic_image("white"))).status_code == 200
    token = client.cookies.get(COOKIE)
    assert client.post(c.LOGOUT, json={}).status_code == 200
    # An independent browser cannot recover the private image with the old token.
    other.cookies.clear()
    other.cookies.set(COOKIE, token)
    for route in (c.FLOORPLAN, c.FLOORPLAN_IMAGE, c.EXPORT):
        response = other.get(route)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
    assert other.put(c.FLOORPLAN, json=synthetic_plan(1)).status_code == 401


@pytest.mark.parametrize("header", [{"Origin": "null"}, {"Origin": "http://testserver", "Sec-Fetch-Site": "same-site"}])
def test_independent_sibling_origin_cannot_mutate_layout(clients, header):
    (client, _), _ = clients
    assert client.put(c.FLOORPLAN, json=synthetic_plan(), headers=header).status_code == 403
    assert client.get(c.FLOORPLAN).json()["revision"] == 0


def test_independent_import_cli_real_local_http_preserves_existing(tmp_path, monkeypatch, capsys):
    """Exercise the actual importer against a real local ASGI HTTP server."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "import_floorplan.py"
    spec = importlib.util.spec_from_file_location("independent_importer", script)
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    # Never read the household credentials for an isolated integration check.
    monkeypatch.setattr(importer, "dotenv_values", lambda _: {"HOME_PASSWORD": "qa-only"})
    image_path, layout_path = tmp_path / "synthetic.png", tmp_path / "synthetic.json"
    Image.new("RGB", (400, 300), "white").save(image_path)
    layout_path.write_text(json.dumps(synthetic_plan()))
    application = create_app(ReadOnlyGrocy(), object(), tmp_path / "cli.sqlite3", "qa-only", "q" * 40)
    server = uvicorn.Server(uvicorn.Config(application, log_level="error"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            assert server.started
            arguments = [str(script), "--image", str(image_path), "--layout", str(layout_path), "--url", origin]
            monkeypatch.setattr(sys, "argv", arguments)
            importer.main()
            assert "revision 1" in capsys.readouterr().out
            with pytest.raises(SystemExit) as rejected:
                importer.main()
            assert rejected.value.code == 2
            assert "layout already exists" in capsys.readouterr().err
            with httpx.Client(base_url=origin, headers={"Origin": origin}) as client:
                assert client.get(c.FLOORPLAN_IMAGE).status_code == 401
                assert client.post(c.LOGIN, json={"password": "qa-only"}).status_code == 200
                assert client.get(c.FLOORPLAN).json()["revision"] == 1
            monkeypatch.setattr(sys, "argv", arguments + ["--replace"])
            importer.main()
            assert "revision 2" in capsys.readouterr().out
        finally:
            server.should_exit = True
            worker.join(timeout=5)
            assert not worker.is_alive()
