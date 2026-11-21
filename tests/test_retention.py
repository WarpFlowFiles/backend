import sqlite3
import time
from datetime import datetime

import pytest

from app.db import SCHEMA_VERSION, connect, init_db
from app.services.cleanup import cleanup_expired
from app.services.messages import OutgoingMessage, render_text

DAY = 24 * 3600


def expires_in(result: dict) -> float:
    return datetime.fromisoformat(result["expires_at"]).timestamp() - time.time()


@pytest.mark.parametrize("ttl", [3600, DAY, 3 * DAY, 7 * DAY])
def test_storage_period_is_chosen_on_first_send(api, ttl):
    draft = api.create(("a.txt", b"a"))
    result = api.send(draft, ttl=ttl).json()
    assert ttl - 60 < expires_in(result) <= ttl


def test_default_storage_period_is_a_day(api):
    result = api.send(api.create(("a.txt", b"a"))).json()
    assert DAY - 60 < expires_in(result) <= DAY


def test_unknown_storage_period_is_rejected(api):
    response = api.send(api.create(("a.txt", b"a")), ttl=30 * DAY)
    assert response.status_code == 400


def test_later_sends_keep_the_expiry(api):
    draft = api.create(("a.txt", b"a"))
    first = api.send(draft, ttl=3600).json()
    second = api.send(draft, "email", to="a@b.cd", ttl=7 * DAY).json()
    assert second["expires_at"] == first["expires_at"]
    assert api.send(draft, ttl=7 * DAY).json()["expires_at"] == first["expires_at"]


def delivered(api, draft: dict, channel: str = "email", **fields) -> dict:
    to = "a@b.cd" if channel == "email" else "@friend"
    result = api.send(draft, channel, to=to, **fields).json()
    return api.wait_delivery(draft, result["delivery_id"])


def test_delivery_that_carried_the_files_leaves_the_copy_briefly(api, settings):
    draft = api.create(("a.txt", b"a"))
    status = delivered(api, draft, ttl=7 * DAY, burn_after_download=True)
    assert status["code_shared"] is False
    assert 0 < expires_in(status) <= settings.delivered_ttl


def test_delivery_with_a_link_keeps_the_files_for_a_day(api, senders):
    senders["telegram"].linked = True
    draft = api.create(("a.txt", b"a"))
    # The storage choice belongs to QR codes: a link in a message lives the default day.
    status = delivered(api, draft, "telegram", ttl=3600, burn_after_download=True)
    assert status["code_shared"] is True
    assert DAY - 60 < expires_in(status) <= DAY
    code = api.send(draft).json()["code"]
    assert api.client.get(f"/api/uploads/{code}").json()["burn_after_download"] is False


def test_qr_after_a_delivery_sets_its_own_period(api, client):
    draft = api.create(("a.txt", b"a"))
    delivered(api, draft)
    result = api.send(draft, ttl=3 * DAY, burn_after_download=True).json()
    assert 3 * DAY - 60 < expires_in(result) <= 3 * DAY
    assert client.get(f"/api/uploads/{result['code']}").json()["burn_after_download"] is True
    # The code is out now: later deliveries leave it alone.
    status = delivered(api, draft)
    assert status["code_shared"] is True
    assert expires_in(status) > 3 * DAY - 60


def test_shared_code_survives_a_delivery(api):
    draft = api.create(("a.txt", b"a"))
    api.send(draft, ttl=DAY)
    status = delivered(api, draft)
    assert expires_in(status) > DAY - 60


def test_resend_after_a_delivery_starts_the_period_over(api):
    draft = api.create(("a.txt", b"a"))
    delivered(api, draft)
    result = api.send(draft, "telegram", to="@friend").json()
    assert DAY - 60 < expires_in(result) <= DAY


def get_file(client, code: str, file_id: str, **params):
    return client.get(f"/api/uploads/{code}/files/{file_id}", params=params)


def test_burn_after_download(client, api):
    code = api.share(("a.txt", b"hello"), ("b.txt", b"bye"), burn_after_download=True)
    info = client.get(f"/api/uploads/{code}").json()
    assert info["burn_after_download"] is True
    a, b = info["files"]

    # Previews, thumbnails and ranges short of the end don't count.
    assert get_file(client, code, a["id"], preview=True).status_code == 200
    assert client.get(f"/api/uploads/{code}/files/{a['id']}", headers={"Range": "bytes=0-1"}).status_code == 206
    assert client.get(f"/api/uploads/{code}").json()["files"][0]["downloaded"] is False

    # A range reaching the last byte finishes the download (phone download managers).
    response = client.get(f"/api/uploads/{code}/files/{a['id']}", headers={"Range": "bytes=2-"})
    assert response.status_code == 206 and response.content == b"llo"
    assert [f["downloaded"] for f in client.get(f"/api/uploads/{code}").json()["files"]] == [True, False]
    assert get_file(client, code, a["id"]).status_code == 410
    assert get_file(client, code, a["id"], preview=True).status_code == 410

    assert get_file(client, code, b["id"]).content == b"bye"
    assert client.get(f"/api/uploads/{code}").status_code == 404


def pickup(api, draft: dict, token: str | None = None):
    headers = {"X-Owner-Token": token} if token else api.owner(draft)
    return api.client.get(f"/api/uploads/{draft['id']}/pickup", headers=headers)


def test_sender_sees_burned_files_taken(api, settings):
    draft = api.create(("a.txt", b"hello"), ("b.txt", b"bye"))
    code = api.send(draft, burn_after_download=True).json()["code"]
    a, b = api.client.get(f"/api/uploads/{code}").json()["files"]
    assert pickup(api, draft).json() == {"downloaded": 0, "taken_at": None}

    get_file(api.client, code, a["id"])
    assert pickup(api, draft).json() == {"downloaded": 1, "taken_at": None}

    get_file(api.client, code, b["id"])
    # Still known after the cleanup removed the upload; only to its owner.
    cleanup_expired(settings, time.time() + 1)
    status = pickup(api, draft).json()
    assert status["taken_at"] is not None
    assert pickup(api, draft, token="wrong").status_code == 403

    cleanup_expired(settings, time.time() + settings.taken_ttl + 1)
    assert pickup(api, draft).status_code == 404


def test_sender_sees_burned_text_read(api):
    draft = api.create()
    code = api.send(draft, text="секрет", burn_after_download=True).json()["code"]
    assert pickup(api, draft).json()["taken_at"] is None
    api.client.get(f"/api/uploads/{code}")
    first = pickup(api, draft).json()["taken_at"]
    api.client.get(f"/api/uploads/{code}")
    assert first is not None and pickup(api, draft).json()["taken_at"] == first


def test_text_burns_after_reading(client, api, settings):
    code = api.share(text="секрет", burn_after_download=True)
    info = client.get(f"/api/uploads/{code}").json()
    assert info["text"] == "секрет" and info["burn_after_download"] is True
    assert 0 < expires_in(info) <= settings.burn_read_ttl
    # A reload within the minute still shows it, without extending the time.
    again = client.get(f"/api/uploads/{code}").json()
    assert again["expires_at"] <= info["expires_at"]


def test_files_stay_without_burn(client, api):
    code = api.share(("a.txt", b"hello"))
    [file] = client.get(f"/api/uploads/{code}").json()["files"]
    for _ in range(2):
        assert get_file(client, code, file["id"]).content == b"hello"
    assert client.get(f"/api/uploads/{code}").json()["burn_after_download"] is False



def test_link_note_in_messages():
    message = OutgoingMessage(
        recipient="a@b.cd",
        from_name="Маша",
        text="",
        code="123456",
        link="https://warpflow.ru/r/123456",
        expires_at=time.time() + 3 * DAY,
        files=[],
        burn_after_download=True,
    )
    text = render_text(message, include_link=True)
    assert "Ссылка действует 3 дня." in text
    assert "Файлы удалятся после скачивания." in text


def test_migrates_schema_v2(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE uploads (id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, ip TEXT NOT NULL,
            owner_token_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
            text TEXT NOT NULL DEFAULT '', from_name TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL, expires_at REAL NOT NULL);
        CREATE TABLE files (id TEXT PRIMARY KEY, upload_id TEXT NOT NULL, original_name TEXT NOT NULL,
            size INTEGER NOT NULL);
        INSERT INTO uploads VALUES ('u', '123456', 'ip', 'h', 'sent', '', '', 0, 1);
        PRAGMA user_version = 2;
        """
    )
    conn.close()

    init_db(path)

    with connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT burn_after_download FROM uploads").fetchone()[0] == 0
        conn.execute("SELECT downloaded_at FROM files")
