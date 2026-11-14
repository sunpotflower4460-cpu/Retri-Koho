import base64
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
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


@pytest.mark.parametrize(
    "configured, expected",
    [
        ("HTTPS://CHAT.EXAMPLE.COM:443/", "https://chat.example.com"),
        ("https://CHAT.EXAMPLE.COM:8443", "https://chat.example.com:8443"),
        ("HTTP://LOCALHOST:80", "http://localhost"),
        ("http://[::1]:8000", "http://[::1]:8000"),
    ],
)
def test_browser_origin_and_secure_cookie(monkeypatch, tmp_path, configured, expected):
    monkeypatch.setenv("CHAT_ORIGIN", configured)
    monkeypatch.setenv("CHAT_DATA_DIR", str(tmp_path))
    for name, letter in (("OWNER", "o"), ("MUSE", "m"), ("DOTS", "d")):
        monkeypatch.setenv(f"CHAT_{name}_TOKEN", letter * 40)
    settings = Settings.from_env()
    assert settings.origin == expected
    # Starlette's test transport cannot parse an IPv6 URL; Origin still carries it.
    base_url = (
        "https://testserver" if expected.startswith("https:") else "http://testserver"
    )
    with TestClient(create_app(settings), base_url=base_url) as client:
        response = client.post(
            "/api/login",
            headers={"X-Chat-Request": "1", "Origin": expected},
            json={"token": settings.tokens["owner"]},
        )
        assert response.status_code == 200
        assert ("Secure" in response.headers["set-cookie"]) == expected.startswith(
            "https:"
        )
        assert client.get("/api/me").json()["author"] == "owner"


@pytest.mark.parametrize(
    "origin", ["https://example.com:bad", "https://example.com:65536"]
)
def test_invalid_origin_port_is_rejected(monkeypatch, origin):
    monkeypatch.setenv("CHAT_ORIGIN", origin)
    with pytest.raises(RuntimeError, match="invalid host or port"):
        Settings.from_env()


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


PAIRING = "/api/pairing"


def request_pairing(client):
    response = client.post(f"{PAIRING}/requests")
    assert response.status_code == 201
    return response.json()


def pairing_url(pairing, action):
    return f"{PAIRING}/requests/{pairing['pairing_id']}/{action}"


def approve_pairing(client, settings, pairing):
    return client.post(
        pairing_url(pairing, "approve"),
        headers=auth(settings, "owner"),
        json={"code": pairing["code"]},
    )


def claim_pairing(client, pairing, code=None):
    return client.post(
        pairing_url(pairing, "claim"),
        json={"code": pairing["code"] if code is None else code},
    )


def test_pairing_end_to_end_and_restart(client, settings):
    pairing = request_pairing(client)
    assert set(pairing) == {"pairing_id", "code", "expires_at"}
    assert len(pairing["pairing_id"]) >= 32
    assert len(pairing["code"]) == 6 and pairing["code"].isascii() and pairing["code"].isdigit()
    expires = datetime.fromisoformat(pairing["expires_at"])
    assert 598 < (expires - datetime.now(UTC)).total_seconds() <= 600
    owner = auth(settings, "owner")
    requests = client.get(f"{PAIRING}/requests", headers=owner).json()["requests"]
    assert len(requests) == 1
    assert requests[0].keys() == pairing.keys() | {"created_at"}
    assert all(requests[0][key] == value for key, value in pairing.items())
    approval = approve_pairing(client, settings, pairing)
    assert approval.status_code == 200
    assert approval.json() == {"ok": True}
    assert client.get(f"{PAIRING}/requests", headers=owner).json() == {"requests": []}
    issued = client.get(f"{PAIRING}/credentials", headers=owner).json()["credentials"]
    assert len(issued) == 1
    assert set(issued[0]) == {"credential_id", "author", "issued_at", "status"}
    assert issued[0]["author"] == "muse" and issued[0]["status"] == "unclaimed"

    # A second app instance reads the existing database and derives delivery
    # using the same private server key.
    with TestClient(create_app(settings)) as restarted:
        claim = claim_pairing(restarted, pairing)
        assert claim.status_code == 200 and claim.headers["cache-control"] == "no-store"
        assert set(claim.json()) == {"author", "token"}
        assert claim.json()["author"] == "muse"
        token = claim.json()["token"]
        valid_token = len(token) >= 32 and token not in settings.tokens.values()
        assert valid_token
        header = {"Authorization": "Bearer " + token}
        assert restarted.get("/api/me", headers=header).json()["author"] == "muse"
        assert restarted.post(
            "/api/messages",
            headers=header,
            data={"body": "paired Muse", "client_id": str(uuid4())},
        ).status_code == 201
        assert restarted.get("/api/messages", headers=header).json()["messages"][0]["author"] == "muse"

    with sqlite3.connect(settings.data_dir / "chat.sqlite3") as db:
        saved = db.execute("SELECT token_hash FROM issued_credentials").fetchone()[0]
        request = db.execute("SELECT code, claim_nonce FROM pairing_requests").fetchone()
    hash_matches = saved == sha256(token.encode()).hexdigest()
    assert hash_matches and request == (None, None)
    assert client.get(f"{PAIRING}/credentials", headers=owner).json()["credentials"][0]["status"] == "active"


def test_pairing_claim_before_approval(client):
    response = claim_pairing(client, request_pairing(client))
    assert response.status_code == 425
    assert response.json() == {"detail": "pending"}


def test_pairing_concurrent_claim_returns_token_once(client, settings):
    pairing = request_pairing(client)
    assert approve_pairing(client, settings, pairing).status_code == 200
    barrier = threading.Barrier(2, timeout=5)

    def simultaneous_claim(requester):
        barrier.wait()
        return claim_pairing(requester, pairing)

    with TestClient(create_app(settings)) as second, ThreadPoolExecutor(2) as workers:
        results = sorted(workers.map(simultaneous_claim, (client, second)), key=lambda item: item.status_code)
    assert [result.status_code for result in results] == [200, 409]
    assert set(results[0].json()) == {"author", "token"}
    assert results[1].json() == {"detail": "claimed"}
    assert claim_pairing(client, pairing).json() == {"detail": "claimed"}


@pytest.mark.parametrize("approved", [False, True])
def test_pairing_expiry(client, settings, approved):
    pairing = request_pairing(client)
    if approved:
        assert approve_pairing(client, settings, pairing).status_code == 200
    with sqlite3.connect(settings.data_dir / "chat.sqlite3") as db:
        db.execute("UPDATE pairing_requests SET expires_at = ?", (int(time.time()) - 1,))
    response = claim_pairing(client, pairing)
    assert response.status_code == 410
    assert response.json() == {"detail": "expired"}
    assert approve_pairing(client, settings, pairing).status_code == 409
    request_pairing(client)
    assert claim_pairing(client, pairing).status_code == 410


def test_pairing_rejected(client, settings):
    pairing = request_pairing(client)
    owner = auth(settings, "owner")
    assert client.post(pairing_url(pairing, "reject"), headers=owner).status_code == 200
    response = claim_pairing(client, pairing)
    assert response.status_code == 403
    assert response.json() == {"detail": "rejected"}
    assert client.get(f"{PAIRING}/credentials", headers=owner).json() == {"credentials": []}


@pytest.mark.parametrize("approved", [False, True])
def test_pairing_five_wrong_codes_invalidate(client, settings, approved):
    pairing = request_pairing(client)
    if approved:
        assert approve_pairing(client, settings, pairing).status_code == 200
    wrong = "000000" if pairing["code"] != "000000" else "000001"
    for _ in range(4):
        response = claim_pairing(client, pairing, wrong)
        assert response.status_code == 401 and response.json() == {"detail": "wrong_code"}
    fifth = claim_pairing(client, pairing, wrong)
    assert fifth.status_code == 429
    assert fifth.json() == {"detail": "too_many_attempts"}
    assert claim_pairing(client, pairing).json() == {"detail": "too_many_attempts"}
    assert approve_pairing(client, settings, pairing).status_code == 409


def test_pairing_owner_authorization_and_csrf(client, settings):
    pairing = request_pairing(client)
    owner = auth(settings, "owner")
    wrong = "000000" if pairing["code"] != "000000" else "000001"
    for role in ("muse", "dots", None):
        headers = auth(settings, role) if role else {"X-Chat-Request": "1"}
        status = 403 if role else 401
        for path in ("requests", "credentials"):
            assert client.get(f"{PAIRING}/{path}", headers=headers).status_code == status
        assert client.post(
            pairing_url(pairing, "approve"), headers=headers, json={"code": pairing["code"]}
        ).status_code == status
        assert client.post(pairing_url(pairing, "reject"), headers=headers).status_code == status
    assert client.post(
        pairing_url(pairing, "approve"), headers=owner, json={"code": wrong}
    ).status_code == 422
    assert client.post(
        "/api/login",
        headers={"X-Chat-Request": "1"},
        json={"token": settings.tokens["owner"]},
    ).status_code == 200
    body = {"code": pairing["code"]}
    assert client.post(pairing_url(pairing, "approve"), json=body).status_code == 403
    assert client.post(
        pairing_url(pairing, "approve"),
        headers={"X-Chat-Request": "1", "Origin": "https://evil.example"},
        json=body,
    ).status_code == 403
    assert client.post(
        pairing_url(pairing, "approve"),
        headers={"X-Chat-Request": "1", "Origin": settings.origin},
        json=body,
    ).status_code == 200


def test_pairing_revocation_blocks_bearer_and_existing_browser_session(client, settings):
    pairing = request_pairing(client)
    assert approve_pairing(client, settings, pairing).status_code == 200
    claim = claim_pairing(client, pairing)
    assert claim.status_code == 200
    token = claim.json()["token"]
    bearer = {"Authorization": "Bearer " + token}
    browser_headers = {"X-Chat-Request": "1"}
    assert client.post("/api/login", headers=browser_headers, json={"token": token}).status_code == 200
    assert client.get("/api/me").json()["author"] == "muse"

    url = f"{PAIRING}/credentials/{pairing['pairing_id']}/revoke"
    assert client.post(url, headers=bearer).status_code == 403
    assert client.post(url, headers=auth(settings, "owner")).status_code == 200
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers=bearer).status_code == 401
    assert client.get("/api/events", headers=bearer).status_code == 401
    assert client.post("/api/login", headers=browser_headers, json={"token": token}).status_code == 401
    for author in settings.tokens:
        assert client.get("/api/me", headers=auth(settings, author)).json()["author"] == author


def test_pairing_revocation_before_claim(client, settings):
    pairing = request_pairing(client)
    assert approve_pairing(client, settings, pairing).status_code == 200
    url = f"{PAIRING}/credentials/{pairing['pairing_id']}/revoke"
    assert client.post(url, headers=auth(settings, "owner")).status_code == 200
    assert claim_pairing(client, pairing).json() == {"detail": "rejected"}


def test_pairing_limits_and_unique_codes(client, settings):
    first, second, third = (request_pairing(client) for _ in range(3))
    assert len({item["code"] for item in (first, second, third)}) == 3
    assert approve_pairing(client, settings, first).status_code == 200
    assert client.post(f"{PAIRING}/requests").status_code == 429
    assert claim_pairing(client, first).status_code == 200
    pairing = request_pairing(client)
    owner = auth(settings, "owner")
    for _ in range(8):
        assert client.post(pairing_url(pairing, "reject"), headers=owner).status_code == 200
        pairing = request_pairing(client)
    assert client.post(pairing_url(pairing, "reject"), headers=owner).status_code == 200
    assert client.post(f"{PAIRING}/requests").json() == {"detail": "pairing_limit"}


def test_secret_fields_are_redacted_from_validation_responses(client):
    secret = "test_credential_value" * 40
    response = client.post("/api/login", headers={"X-Chat-Request": "1"}, json={"token": secret})
    assert response.status_code == 422 and secret not in response.text
    pairing = request_pairing(client)
    response = claim_pairing(client, pairing, secret)
    assert response.status_code == 422 and secret not in response.text
