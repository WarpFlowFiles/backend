from urllib.parse import quote

import pytest


def open_upload(client, code: str, password: str | None = None):
    headers = {"X-Upload-Password": quote(password)} if password is not None else {}
    return client.get(f"/api/uploads/{code}", headers=headers)


def test_password_protects_the_upload_and_its_files(client, api):
    code = api.share(("a.txt", b"hello"), text="секрет", password="пароль-123")

    locked = open_upload(client, code)
    assert locked.status_code == 401 and locked.json() == {"detail": "Введите пароль"}
    assert open_upload(client, code, "неверный").json() == {"detail": "Неверный пароль"}

    info = open_upload(client, code, "пароль-123").json()
    assert info["text"] == "секрет" and info["key"]
    file_id = info["files"][0]["id"]
    url = f"/api/uploads/{code}/files/{file_id}"
    assert client.get(url).status_code == 403
    assert client.get(url, params={"key": "wrong"}).status_code == 403
    assert client.get(url, params={"key": "ключ"}).status_code == 403
    assert client.get(url, params={"key": info["key"]}).content == b"hello"


def test_uploads_without_password_need_no_key(client, api):
    code = api.share(("a.txt", b"hello"))
    info = open_upload(client, code).json()
    assert info["key"] is None
    assert client.get(f"/api/uploads/{code}/files/{info['files'][0]['id']}").content == b"hello"


def test_wrong_passwords_count_towards_the_attempt_limit(client, api, settings):
    code = api.share(text="секрет", password="correct horse")
    for _ in range(settings.max_failed_attempts):
        assert open_upload(client, code, "guess-guess").status_code == 401
    assert open_upload(client, code, "correct horse").status_code == 429


def test_burn_text_is_not_read_before_the_password(client, api):
    draft = api.create()
    code = api.send(draft, text="секрет", burn_after_download=True, password="12345678").json()["code"]
    open_upload(client, code)
    open_upload(client, code, "87654321")
    pickup = client.get(f"/api/uploads/{draft['id']}/pickup", headers=api.owner(draft)).json()
    assert pickup["taken_at"] is None
    assert open_upload(client, code, "12345678").json()["text"] == "секрет"


@pytest.mark.parametrize(
    ("channel", "password", "error"),
    [
        ("qr", "1234567", "Пароль должен быть от 8 до 128 символов"),
        ("qr", "x" * 129, "Пароль должен быть от 8 до 128 символов"),
        ("email", "12345678", "Пароль можно поставить только на QR-код"),
    ],
)
def test_invalid_passwords(api, channel, password, error):
    draft = api.create(("a.txt", b"hello"))
    response = api.send(draft, channel, to="a@b.ru", password=password)
    assert response.status_code == 400 and response.json() == {"detail": error}
