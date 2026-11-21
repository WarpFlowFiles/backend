import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.messages import DeliveryError, OutgoingMessage


class FakeSender:
    def __init__(self) -> None:
        self.available = True
        self.error: str | None = None
        # Whether messages go out with a link to the files (too big to attach).
        self.linked = False
        self.sent: list[OutgoingMessage] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, message: OutgoingMessage) -> bool:
        if self.error:
            raise DeliveryError(self.error)
        self.sent.append(message)
        return self.linked


@pytest.fixture
def settings(tmp_path):
    return Settings(
        storage_dir=tmp_path / "storage",
        public_url="https://warpflow.ru",
        max_file_size=1000,
        ip_quota=1500,
    )


@pytest.fixture
def senders():
    return {"email": FakeSender(), "telegram": FakeSender()}


@pytest.fixture
def client(settings, senders):
    with TestClient(create_app(settings, senders)) as client:
        yield client


class Api:
    """Thin helpers over the HTTP API for tests."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def create(self, *files: tuple[str, bytes]) -> dict:
        if files:
            response = self.client.post("/api/uploads", files=[("files", f) for f in files])
        else:
            response = self.client.post("/api/uploads")
        assert response.status_code == 201, response.text
        return response.json()

    @staticmethod
    def owner(draft: dict) -> dict:
        return {"X-Owner-Token": draft["owner_token"]}

    def add(self, draft: dict, *files: tuple[str, bytes]):
        return self.client.post(
            f"/api/uploads/{draft['id']}/files",
            files=[("files", f) for f in files],
            headers=self.owner(draft),
        )

    def send(self, draft: dict, channel: str = "qr", **fields):
        return self.client.post(
            f"/api/uploads/{draft['id']}/send",
            json={"channel": channel, **fields},
            headers=self.owner(draft),
        )

    def share(self, *files: tuple[str, bytes], **fields) -> str:
        """Creates an upload, sends it via QR and returns the code."""
        draft = self.create(*files)
        response = self.send(draft, **fields)
        assert response.status_code == 200, response.text
        return response.json()["code"]

    def wait_delivery(self, draft: dict, delivery_id: str) -> dict:
        deadline = time.monotonic() + 5
        while True:
            response = self.client.get(
                f"/api/uploads/{draft['id']}/deliveries/{delivery_id}", headers=self.owner(draft)
            )
            assert response.status_code == 200, response.text
            status = response.json()
            if status["status"] != "pending" or time.monotonic() > deadline:
                return status
            time.sleep(0.01)


@pytest.fixture
def api(client):
    return Api(client)
