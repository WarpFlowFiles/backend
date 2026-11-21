import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.stats import main as stats_main
from app.stats.main import create_stats_app

AUTH = ("admin", "secret-stats")


@pytest.fixture
def stats(settings, client, monkeypatch):
    # `client` first: the main app creates the database the stats app reads.
    monkeypatch.setattr(stats_main, "FAILED_LOGIN_DELAY", 0)
    app = create_stats_app(dataclasses.replace(settings, stats_password=AUTH[1], app_url="http://127.0.0.1:9"))
    with TestClient(app) as stats_client:
        yield stats_client


def test_stats_need_the_password(stats, settings):
    assert stats.get("/").status_code == 401
    assert stats.get("/api/stats", auth=("admin", "wrong")).status_code == 401
    assert stats.get("/", auth=AUTH).status_code == 200

    closed = TestClient(create_stats_app(settings))
    assert closed.get("/api/stats", auth=("admin", "")).status_code == 503


def test_stats_count_todays_activity(stats, client, api):
    code = api.share(("a.txt", b"hello"), ("b.txt", b"bye!"), password="12345678")
    draft = api.create(("c.txt", b"x"))
    api.wait_delivery(draft, api.send(draft, "email", to="a@b.ru").json()["delivery_id"])
    client.get("/api/uploads/000000")
    client.get(f"/api/uploads/{code}", headers={"X-Upload-Password": "wrong-one"})
    info = client.get(f"/api/uploads/{code}", headers={"X-Upload-Password": "12345678"}).json()
    file_id = info["files"][0]["id"]
    client.get(f"/api/uploads/{code}/files/{file_id}", params={"key": info["key"], "preview": 1})
    client.get(f"/api/uploads/{code}/files/{file_id}", params={"key": info["key"]})

    data = stats.get("/api/stats", params={"days": 7}, auth=AUTH).json()
    today = data["today"]
    assert today["sent"] == {"qr": 1, "email": 1, "telegram": 0}
    assert today["delivered"] == 1
    assert (today["files_uploaded"], today["bytes_uploaded"]) == (3, 10)
    # The preview does not count as a download.
    assert (today["downloads"], today["bytes_downloaded"]) == (1, 5)
    assert (today["opened"], today["code_failed"], today["password_failed"]) == (1, 1, 1)
    assert today["visitors"] == 1

    assert len(data["days"]) == 7 and data["days"][-1]["sent"]["qr"] == 1
    assert len(data["hours"]) == 24 and sum(h["sent"]["email"] for h in data["hours"]) == 1
    assert data["usage"] == {"qr": 1, "with_password": 1, "burn": 0}
    assert data["stored"]["files"] == 3 and data["stored"]["with_password"] == 1
    assert data["server"]["disk"]["total"] > 0
    # The main app is not running at that address.
    assert data["app"] == {"up": False}


def test_health(client):
    assert client.get("/api/health").json() == {
        "channels": {"email": True, "telegram": True},
        "uploading_bytes": 0,
    }
