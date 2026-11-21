import pytest


def test_email_delivery(client, api, senders):
    draft = api.create(("a.txt", b"hello"))
    response = api.send(draft, "email", to=" user@example.com ", text="Привет", from_name="Маша")
    assert response.status_code == 200
    result = response.json()

    status = api.wait_delivery(draft, result["delivery_id"])
    assert status["status"] == "sent"
    assert status["recipient"] == "user@example.com"

    [message] = senders["email"].sent
    assert message.recipient == "user@example.com"
    assert message.from_name == "Маша"
    assert message.text == "Привет"
    assert message.link == f"https://warpflow.ru/r/{result['code']}"
    assert [(f.name, f.path.read_bytes()) for f in message.files] == [("a.txt", b"hello")]

    assert client.get(f"/api/uploads/{result['code']}").status_code == 200


@pytest.mark.parametrize("to", ["monk_42", "@monk_42"])
def test_telegram_username_is_normalized(api, senders, to):
    draft = api.create(("a.txt", b"a"))
    result = api.send(draft, "telegram", to=to).json()

    assert api.wait_delivery(draft, result["delivery_id"])["status"] == "sent"
    assert senders["telegram"].sent[0].recipient == "@monk_42"


@pytest.mark.parametrize(
    "fields",
    [
        {"channel": "fax"},
        {"channel": "email", "to": "not-an-email"},
        {"channel": "email"},
        {"channel": "telegram", "to": "@ab"},
        {"channel": "telegram", "to": "@bad-name"},
        {"channel": "qr", "text": "x" * 4001},
        {"channel": "qr", "from_name": "x" * 65},
    ],
)
def test_invalid_send_requests(api, fields):
    response = api.send(api.create(("a.txt", b"a")), **fields)
    assert response.status_code == 400


def test_unavailable_channel(api, senders):
    senders["telegram"].available = False
    response = api.send(api.create(("a.txt", b"a")), "telegram", to="@monk")
    assert response.status_code == 503


def test_delivery_error_is_reported(api, senders):
    senders["telegram"].error = "Пользователь @monk не найден в Telegram"
    draft = api.create(("a.txt", b"a"))
    result = api.send(draft, "telegram", to="@monk").json()

    status = api.wait_delivery(draft, result["delivery_id"])
    assert status["status"] == "failed"
    assert status["error"] == "Пользователь @monk не найден в Telegram"


def test_send_limit_per_channel(client, api):
    for _ in range(5):
        draft = api.create()
        assert api.send(draft, "email", to="a@b.cd", text="hi").status_code == 200
        client.delete(f"/api/uploads/{draft['id']}", headers=api.owner(draft))

    draft = api.create()
    response = api.send(draft, "email", to="a@b.cd", text="hi")
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0

    assert api.send(draft, "telegram", to="@monk", text="hi").status_code == 200
    assert api.send(draft, "qr").status_code == 200


def test_delivery_status_requires_owner(client, api):
    draft = api.create(("a.txt", b"a"))
    result = api.send(draft, "email", to="a@b.cd").json()

    url = f"/api/uploads/{draft['id']}/deliveries/{result['delivery_id']}"
    assert client.get(url, headers={"X-Owner-Token": "wrong"}).status_code == 403
    assert client.get(f"/api/uploads/{draft['id']}/deliveries/nope", headers=api.owner(draft)).status_code == 404


def test_send_retry_after_waits_for_all_extra_sends():
    import sqlite3

    from app.config import Settings
    from app.services.limits import SEND_WINDOW, send_retry_after

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE deliveries (ip TEXT, channel TEXT, created_at REAL)")
    now = 10_000.0
    # 7 sends 50..20 minutes ago, while the limit is 5: two extra sends must expire first.
    for minutes_ago in (50, 45, 40, 35, 30, 25, 20):
        conn.execute("INSERT INTO deliveries VALUES ('1.2.3.4', 'telegram', ?)", (now - minutes_ago * 60,))
    settings = Settings(max_sends_per_hour=5)

    assert send_retry_after(conn, "1.2.3.4", "telegram", now, settings) == SEND_WINDOW - 40 * 60
    assert send_retry_after(conn, "1.2.3.4", "email", now, settings) is None
    assert send_retry_after(conn, "1.2.3.4", "telegram", now + 20 * 60 + 1, settings) is None
