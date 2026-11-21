import time
from urllib.parse import quote

from app.db import connect
from app.services.cleanup import cleanup_expired


def expire_all(settings):
    with connect(settings.db_path) as conn:
        conn.execute("UPDATE uploads SET expires_at = ?", (time.time() - 1,))


def code_of(settings, draft):
    with connect(settings.db_path) as conn:
        return conn.execute("SELECT code FROM uploads WHERE id = ?", (draft["id"],)).fetchone()[0]


def test_share_and_download_single_file(client, api):
    code = api.share(("отчёт.txt", b"hello"))

    assert len(code) == 6 and code.isdigit()
    info = client.get(f"/api/uploads/{code}").json()
    assert info["from_name"] == "Аноним"
    assert info["text"] == ""
    assert [(f["name"], f["size"]) for f in info["files"]] == [("отчёт.txt", 5)]

    response = client.get(f"/api/uploads/{code}/files/{info['files'][0]['id']}")
    assert response.status_code == 200
    assert response.content == b"hello"
    assert quote("отчёт.txt") in response.headers["content-disposition"]


def test_media_is_served_with_its_type_and_ranges(client, api):
    code = api.share(("clip.mp4", b"0123456789"), ("song.mp3", b"x"), ("page.html", b"<script>"), ("logo.svg", b"<svg/>"))
    files = {f["name"]: f["id"] for f in client.get(f"/api/uploads/{code}").json()["files"]}

    def get(name, **headers):
        return client.get(f"/api/uploads/{code}/files/{files[name]}", headers=headers)

    assert get("clip.mp4").headers["content-type"] == "video/mp4"
    assert get("song.mp3").headers["content-type"] == "audio/mpeg"
    assert get("page.html").headers["content-type"] == "application/octet-stream"
    assert get("logo.svg").headers["content-type"] == "application/octet-stream"
    assert get("clip.mp4").headers["x-content-type-options"] == "nosniff"
    assert get("clip.mp4").headers["content-disposition"].startswith("attachment")

    partial = get("clip.mp4", Range="bytes=2-5")
    assert partial.status_code == 206
    assert partial.content == b"2345"


def test_share_multiple_files(client, api):
    code = api.share(("a.txt", b"aaa"), ("b.bin", b"\x00" * 10))

    info = client.get(f"/api/uploads/{code}").json()
    assert [(f["name"], f["size"]) for f in info["files"]] == [("a.txt", 3), ("b.bin", 10)]
    for f, content in zip(info["files"], [b"aaa", b"\x00" * 10]):
        assert client.get(f"/api/uploads/{code}/files/{f['id']}").content == content


def test_text_only_upload(client, api):
    draft = api.create()
    assert draft["files"] == []

    response = api.send(draft, text="  Привет!\nВторая строка ", from_name="Маша")
    info = client.get(f"/api/uploads/{response.json()['code']}").json()

    assert info["text"] == "Привет!\nВторая строка"
    assert info["from_name"] == "Маша"
    assert info["files"] == []


def test_empty_upload_cannot_be_sent(api):
    response = api.send(api.create(), text="   ")
    assert response.status_code == 400


def test_draft_is_not_available_by_code(client, api, settings):
    draft = api.create(("a.txt", b"a"))
    assert client.get(f"/api/uploads/{code_of(settings, draft)}").status_code == 404


def test_add_and_delete_files_in_draft(client, api):
    draft = api.create(("a.txt", b"a"))
    added = api.add(draft, ("b.txt", b"bb"), ("c.txt", b"ccc"))
    assert added.status_code == 201
    assert [f["name"] for f in added.json()["files"]] == ["b.txt", "c.txt"]

    b_id = added.json()["files"][0]["id"]
    response = client.delete(f"/api/uploads/{draft['id']}/files/{b_id}", headers=api.owner(draft))
    assert response.status_code == 204

    code = api.send(draft).json()["code"]
    info = client.get(f"/api/uploads/{code}").json()
    assert [f["name"] for f in info["files"]] == ["a.txt", "c.txt"]


def test_sent_upload_is_frozen(client, api):
    draft = api.create(("a.txt", b"a"))
    api.send(draft, text="первый")

    assert api.add(draft, ("b.txt", b"b")).status_code == 409
    file_id = draft["files"][0]["id"]
    response = client.delete(f"/api/uploads/{draft['id']}/files/{file_id}", headers=api.owner(draft))
    assert response.status_code == 409

    code = api.send(draft, text="второй").json()["code"]
    assert client.get(f"/api/uploads/{code}").json()["text"] == "первый"


def test_owner_token_is_required(client, api):
    draft = api.create(("a.txt", b"a"))
    for headers in ({}, {"X-Owner-Token": "wrong"}):
        assert client.post(f"/api/uploads/{draft['id']}/send", json={"channel": "qr"}, headers=headers).status_code == 403
        assert client.post(f"/api/uploads/{draft['id']}/files", files=[("files", ("b", b"b"))], headers=headers).status_code == 403
        assert client.delete(f"/api/uploads/{draft['id']}", headers=headers).status_code == 403


def test_cancel_makes_upload_unavailable_and_frees_quota(client, api):
    draft = api.create(("a.bin", b"x" * 900))
    code = api.send(draft).json()["code"]

    assert client.delete(f"/api/uploads/{draft['id']}", headers=api.owner(draft)).status_code == 204
    assert client.get(f"/api/uploads/{code}").status_code == 404
    api.create(("b.bin", b"x" * 900))


def test_lifetimes(api, settings):
    draft = api.create(("a.txt", b"a"))
    with connect(settings.db_path) as conn:
        draft_expires = conn.execute("SELECT expires_at FROM uploads").fetchone()[0]
    assert abs(draft_expires - time.time() - settings.draft_ttl) < 5

    api.send(draft)
    with connect(settings.db_path) as conn:
        sent_expires = conn.execute("SELECT expires_at FROM uploads").fetchone()[0]
    assert abs(sent_expires - time.time() - settings.sent_ttl) < 5


def test_filename_is_reduced_to_basename(client, api):
    code = api.share(("../../etc/passwd", b"x"))
    assert client.get(f"/api/uploads/{code}").json()["files"][0]["name"] == "passwd"


def test_bad_upload_requests_are_rejected(client, api):
    response = client.post("/api/uploads", content=b"raw", headers={"content-type": "text/plain"})
    assert response.status_code == 400

    draft = api.create()
    assert api.add(draft, ("", b"")).status_code == 400


def test_file_over_size_limit_is_rejected(client, settings):
    response = client.post("/api/uploads", files=[("files", ("big.bin", b"x" * 1001))])

    assert response.status_code == 413
    assert list(settings.tmp_dir.iterdir()) == []
    assert list(settings.files_dir.iterdir()) == []


def test_ip_quota_counts_active_uploads(client, api, settings):
    draft = api.create(("a.bin", b"x" * 900))

    assert api.add(draft, ("b.bin", b"x" * 900)).status_code == 413
    response = client.post("/api/uploads", files=[("files", ("b.bin", b"x" * 900))])
    assert response.status_code == 413
    assert list(settings.tmp_dir.iterdir()) == []

    expire_all(settings)
    api.create(("b.bin", b"x" * 900))


def test_quota_applies_to_total_of_one_request(client):
    files = [("files", ("a.bin", b"x" * 800)), ("files", ("b.bin", b"x" * 800))]
    assert client.post("/api/uploads", files=files).status_code == 413


def test_unknown_or_malformed_code_returns_404(client):
    assert client.get("/api/uploads/000000").status_code == 404
    assert client.get("/api/uploads/abc").status_code == 404


def test_expired_upload_returns_404(client, api, settings):
    code = api.share(("a.txt", b"a"))
    expire_all(settings)
    assert client.get(f"/api/uploads/{code}").status_code == 404


def test_unknown_file_id_returns_404(client, api):
    code = api.share(("a.txt", b"a"))
    assert client.get(f"/api/uploads/{code}/files/nope").status_code == 404


def test_too_many_failed_attempts_block_ip(client, api):
    code = api.share(("a.txt", b"a"))
    for _ in range(10):
        assert client.get("/api/uploads/abcdef").status_code == 404

    response = client.get(f"/api/uploads/{code}")
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert client.get(f"/api/uploads/{code}/files/x").status_code == 429


def test_cleanup_removes_only_expired_uploads(client, api, settings):
    old = api.share(("old.txt", b"old"))
    expire_all(settings)
    new = api.share(("new.txt", b"new"))

    assert cleanup_expired(settings, time.time()) == 1

    with connect(settings.db_path) as conn:
        codes = [row["code"] for row in conn.execute("SELECT code FROM uploads")]
        file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert codes == [new]
    assert file_count == 1
    assert len(list(settings.files_dir.iterdir())) == 1
    assert client.get(f"/api/uploads/{old}").status_code == 404
