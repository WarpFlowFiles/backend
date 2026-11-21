def deliver(api, to: str = "@Monk") -> dict:
    draft = api.create(("a.txt", b"a"))
    result = api.send(draft, "telegram", to=to).json()
    assert api.wait_delivery(draft, result["delivery_id"])["status"] == "sent"
    return draft


def avatar(client, api, draft: dict, to: str = "@monk"):
    return client.get(f"/api/uploads/{draft['id']}/avatar", params={"to": to}, headers=api.owner(draft))


def test_sender_gets_the_recipients_photo_once_per_day(client, api, senders):
    calls = []

    async def profile_photo(username):
        calls.append(username)
        return b"jpeg"

    senders["telegram"].profile_photo = profile_photo
    draft = deliver(api)

    for _ in range(2):
        response = avatar(client, api, draft)
        assert response.status_code == 200
        assert response.content == b"jpeg" and response.headers["content-type"] == "image/jpeg"
    assert calls == ["@monk"]


def test_no_photo_is_404_and_cached(client, api, senders):
    calls = []

    async def profile_photo(username):
        calls.append(username)
        return None

    senders["telegram"].profile_photo = profile_photo
    draft = deliver(api)
    assert avatar(client, api, draft).status_code == 404
    assert avatar(client, api, draft).status_code == 404
    assert calls == ["@monk"]


def test_only_for_recipients_of_this_upload(client, api, senders):
    async def profile_photo(username):
        return b"jpeg"

    senders["telegram"].profile_photo = profile_photo
    draft = deliver(api)

    assert avatar(client, api, draft, "@durov").status_code == 404
    response = client.get(f"/api/uploads/{draft['id']}/avatar", params={"to": "@monk"}, headers={"X-Owner-Token": "x"})
    assert response.status_code == 403


def test_failed_delivery_gives_no_photo(client, api, senders):
    senders["telegram"].error = "Пользователь @monk не найден в Telegram"
    draft = api.create(("a.txt", b"a"))
    result = api.send(draft, "telegram", to="@monk").json()
    assert api.wait_delivery(draft, result["delivery_id"])["status"] == "failed"
    assert avatar(client, api, draft).status_code == 404
