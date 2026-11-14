import base64
import socket
import sqlite3
import threading
import time
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from realtime.app import MAX_FILE_BYTES, MAX_REQUEST_BYTES, Settings, create_app

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        tmp_path,
        "http://testserver",
        {"owner": "o" * 40, "muse": "m" * 40, "dots": "d" * 40},
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def auth(settings, author="muse"):
    return {"Authorization": f"Bearer {settings.tokens[author]}"}


def post(
    client, settings, body="Hello dots", files=None, client_id=None, author="muse"
):
    return client.post(
        "/api/messages",
        headers=auth(settings, author),
        data={"body": body, "client_id": client_id or str(uuid4())},
        files=files,
    )


def test_no_default_or_shared_keys(monkeypatch):
    for name in ("OWNER", "MUSE", "DOTS"):
        monkeypatch.delenv(f"CHAT_{name}_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        Settings.from_env()
    for name in ("OWNER", "MUSE", "DOTS"):
        monkeypatch.setenv(f"CHAT_{name}_TOKEN", "same" * 12)
    with pytest.raises(RuntimeError):
        Settings.from_env()


def test_history_uploads_and_events_are_private(client):
    for path in ("/api/me", "/api/messages", "/api/events", "/api/attachments/nope"):
        assert client.get(path).status_code == 401
    assert (
        client.post("/api/messages", headers={"X-Chat-Request": "1"}).status_code == 401
    )
    assert client.get("/").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}


def test_login_logout_and_csrf(client, settings):
    assert (
        client.post("/api/login", json={"token": settings.tokens["owner"]}).status_code
        == 403
    )
    headers = {"X-Chat-Request": "1", "Origin": settings.origin}
    assert (
        client.post("/api/login", headers=headers, json={"token": "wrong"}).status_code
        == 401
    )
    response = client.post(
        "/api/login", headers=headers, json={"token": settings.tokens["owner"]}
    )
    assert response.json()["author"] == "owner"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/api/me").json()["author"] == "owner"
    assert (
        client.post(
            "/api/logout", headers={**headers, "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.get("/api/me").status_code == 401


def test_https_cookie_is_secure(tmp_path):
    settings = Settings(
        tmp_path,
        "https://testserver",
        {"owner": "o" * 40, "muse": "m" * 40, "dots": "d" * 40},
    )
    with TestClient(create_app(settings), base_url=settings.origin) as client:
        response = client.post(
            "/api/login",
            headers={"X-Chat-Request": "1"},
            json={"token": settings.tokens["dots"]},
        )
        assert "Secure" in response.headers["set-cookie"]
        assert client.get("/api/me").json()["author"] == "dots"


def test_expired_and_rotated_sessions(client, settings):
    client.post(
        "/api/login",
        headers={"X-Chat-Request": "1"},
        json={"token": settings.tokens["muse"]},
    )
    original = settings.tokens["muse"]
    settings.tokens["muse"] = "new" * 16
    assert client.get("/api/me").status_code == 401
    settings.tokens["muse"] = original
    with sqlite3.connect(settings.data_dir / "chat.sqlite3") as conn:
        conn.execute("UPDATE sessions SET expires_at = 0")
    assert client.get("/api/me").status_code == 401


def test_fixed_identity_idempotency_and_persistence(client, settings):
    key = str(uuid4())
    first = post(
        client, settings, body="こんにちは dots\n画像も共有できます", client_id=key
    )
    assert first.status_code == 201
    assert first.json()["author"] == "muse"
    retry = post(client, settings, client_id=key)
    assert retry.status_code == 200
    assert retry.json() == first.json()
    other = post(client, settings, body="受信しました", author="dots", client_id=key)
    assert other.json()["author"] == "dots"
    with TestClient(create_app(settings)) as restarted:
        history = restarted.get("/api/messages", headers=auth(settings, "owner")).json()
        assert [m["body"] for m in history["messages"]] == [
            first.json()["body"],
            "受信しました",
        ]


def test_attachment_preview_download_and_no_public_path(client, settings):
    result = post(
        client,
        settings,
        body="",
        files=[
            ("files", ("../image.png", PNG, "text/html")),
            ("files", ("draft.txt", b"draft", "text/plain")),
        ],
    ).json()
    image, text = result["attachments"]
    assert image["name"] == "image.png"
    assert image["media_type"] == "image/png"
    url = f"/api/attachments/{image['id']}"
    assert client.get(url).status_code == 401
    preview = client.get(url, headers=auth(settings, "dots"))
    assert preview.content == PNG
    assert preview.headers["content-disposition"].startswith("inline")
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert preview.headers["cache-control"] == "no-store"
    assert (
        client.get(f"{url}?download=true", headers=auth(settings))
        .headers["content-disposition"]
        .startswith("attachment")
    )
    download = client.get(f"/api/attachments/{text['id']}", headers=auth(settings))
    assert download.content == b"draft"
    assert download.headers["content-disposition"].startswith("attachment")
    assert client.get(f"/uploads/{image['id']}").status_code == 404
    # A retry containing files must not leave orphaned copies.
    assert len(list((settings.data_dir / "uploads").iterdir())) == 2


def test_active_content_is_downloaded_never_previewed(client, settings):
    html = b"<svg onload='alert(1)'></svg>"
    attachment = post(
        client, settings, files=[("files", ("bad.png", html, "image/png"))]
    ).json()["attachments"][0]
    response = client.get(
        f"/api/attachments/{attachment['id']}", headers=auth(settings)
    )
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize("body", ["", "x" * 4001])
def test_invalid_message(client, settings, body):
    assert post(client, settings, body=body).status_code == 422
    assert client.get("/api/messages", headers=auth(settings)).json()["messages"] == []


def test_upload_limits_clean_up_partial_files(client, settings):
    response = post(
        client,
        settings,
        files=[
            ("files", ("ok.txt", b"ok")),
            ("files", ("big.bin", b"x" * (MAX_FILE_BYTES + 1))),
        ],
    )
    assert response.status_code == 413
    assert list((settings.data_dir / "uploads").iterdir()) == []
    assert client.get("/api/messages", headers=auth(settings)).json()["messages"] == []
    too_many = post(
        client, settings, files=[("files", (f"{i}.txt", b"x")) for i in range(6)]
    )
    assert too_many.status_code == 400


def test_total_request_limit_even_without_content_length(client, settings):
    headers = {**auth(settings), "Content-Type": "multipart/form-data; boundary=test"}
    assert (
        client.post(
            "/api/messages",
            headers={**headers, "Content-Length": str(MAX_REQUEST_BYTES + 1)},
            content=b"",
        ).status_code
        == 413
    )

    def chunks():
        yield b'--test\r\nContent-Disposition: form-data; name="files"; filename="x.bin"\r\n\r\n'
        for _ in range(22):
            yield b"x" * 1024 * 1024
        yield b"\r\n--test--\r\n"

    assert (
        client.post("/api/messages", headers=headers, content=chunks()).status_code
        == 413
    )


def test_pagination_and_invalid_cursors(client, settings):
    for i in range(5):
        post(client, settings, body=str(i))
    newest = client.get("/api/messages?limit=2", headers=auth(settings)).json()
    assert newest["has_more"]
    assert [m["body"] for m in newest["messages"]] == ["3", "4"]
    earlier = client.get(
        f"/api/messages?before={newest['messages'][0]['id']}&limit=3",
        headers=auth(settings),
    ).json()
    assert not earlier["has_more"]
    assert [m["body"] for m in earlier["messages"]] == ["0", "1", "2"]
    for query in ("limit=101", "before=0", "before=99999999999999999999999"):
        assert (
            client.get(f"/api/messages?{query}", headers=auth(settings)).status_code
            == 422
        )
    assert client.get("/api/events?after=-1", headers=auth(settings)).status_code == 422


def test_live_event_delivery_and_reconnect(settings):
    # A real TCP connection verifies streaming, not a buffered test-client response.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), log_level="error", lifespan="off")
    )
    worker = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    worker.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        with (
            httpx.Client(base_url=url, timeout=5) as writer,
            httpx.Client(base_url=url, timeout=5) as reader,
        ):
            first = post(writer, settings, body="first").json()
            with reader.stream(
                "GET",
                f"/api/events?after={first['id']}",
                headers=auth(settings, "dots"),
            ) as response:
                assert response.status_code == 200
                second = post(
                    writer,
                    settings,
                    body="リアルタイム",
                    files=[("files", ("image.png", PNG, "image/png"))],
                ).json()
                lines = response.iter_lines()
                assert (
                    next(line for line in lines if line.startswith("id:"))
                    == f"id: {second['id']}"
                )
                assert next(lines) == "event: message"
                assert "リアルタイム" in next(lines)
            third = post(writer, settings, body="during disconnect").json()
            with reader.stream(
                "GET",
                "/api/events?after=0",
                headers={**auth(settings), "Last-Event-ID": str(second["id"])},
            ) as response:
                assert (
                    next(
                        line for line in response.iter_lines() if line.startswith("id:")
                    )
                    == f"id: {third['id']}"
                )
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        sock.close()
    assert not worker.is_alive()
