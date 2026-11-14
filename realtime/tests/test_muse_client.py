import json
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
import uvicorn

from realtime.app import Settings, create_app

OWNER = "fixture-owner-" * 4


@pytest.fixture
def room(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = Settings(
        tmp_path / "room", origin,
        {"owner": OWNER, "muse": "fixture-muse-" * 4, "dots": "fixture-dots-" * 4},
    )
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="error"))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        with httpx.Client(base_url=origin, timeout=5) as http:
            download = http.get("/pairing/muse-client.py")
            assert download.status_code == 200
            assert "attachment;" in download.headers["content-disposition"]
            assert download.headers["cache-control"] == "no-store"
            helper = tmp_path / "retri-muse.py"
            helper.write_bytes(download.content)
            yield origin, http, helper
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        sock.close()


def invoke(helper, state, *arguments, body=None):
    return subprocess.run(
        [sys.executable, str(helper), "--state-dir", str(state), *arguments],
        input=body,
        text=True,
        capture_output=True,
        timeout=15,
    )


def submit_approval(http, approval, action="approve"):
    link = urlsplit(approval["approve_url"])
    path = link.path.replace("/approve/", f"/{action}/")
    return http.post(path, data={"t": parse_qs(link.query)["t"][0]})


def test_muse_client_restores_pairing_and_reuses_credential(room, tmp_path):
    origin, http, helper = room
    state = tmp_path / "private"
    requested = invoke(helper, state, "--url", origin, "history")
    assert requested.returncode == 2
    approval = json.loads(requested.stdout)
    assert set(approval) == {"status", "approve_url", "expires_at"}
    assert approval["status"] == "approval_required"
    session_path = state / "session.json"
    pending = json.loads(session_path.read_text())["pending"]
    assert pending["pairing_id"] not in requested.stdout
    assert state.stat().st_mode & 0o777 == 0o700
    assert session_path.stat().st_mode & 0o777 == 0o600
    again = invoke(helper, state, "connect")
    assert again.returncode == 2 and json.loads(again.stdout) == approval

    with ThreadPoolExecutor(2) as workers:
        connections = [workers.submit(invoke, helper, state, "connect", "--wait", "8") for _ in range(2)]
        assert submit_approval(http, approval).status_code == 200
        for future in connections:
            result = future.result(timeout=10)
            assert result.returncode == 0
            assert json.loads(result.stdout.splitlines()[-1]) == {"status": "connected", "author": "muse"}

    credential = json.loads(session_path.read_text())
    assert set(credential) == {"origin", "token"}
    assert session_path.stat().st_mode & 0o777 == 0o600
    history = invoke(helper, state, "history")
    assert history.returncode == 0
    assert json.loads(history.stdout) == {"messages": [], "has_more": False}

    identifier = str(uuid4())
    posted = invoke(helper, state, "post", "--client-id", identifier, body="確認用")
    retried = invoke(helper, state, "post", "--client-id", identifier, body="確認用")
    assert posted.returncode == retried.returncode == 0
    message = json.loads(posted.stdout)
    assert json.loads(retried.stdout) == message and message["author"] == "muse"
    history = invoke(helper, state, "history")
    assert json.loads(history.stdout)["messages"] == [message]

    other_origin = invoke(helper, state, "--url", "http://127.0.0.1:1", "connect")
    assert other_origin.returncode == 1
    assert "URL が異なります" in json.loads(other_origin.stdout)["message"]
    claim_id = pending["pairing_id"]
    revoked = http.post(
        f"/api/pairing/credentials/{claim_id}/revoke", headers={"Authorization": "Bearer " + OWNER}
    )
    assert revoked.status_code == 200
    reconnected = invoke(helper, state, "history")
    assert reconnected.returncode == 2
    assert json.loads(reconnected.stdout)["approve_url"] != approval["approve_url"]
    leak = any(credential["token"] in output for output in (
        history.stdout, posted.stdout, retried.stdout, reconnected.stdout, other_origin.stdout
    ))
    assert not leak


def test_muse_client_requires_an_explicit_retry_after_rejection(room, tmp_path):
    origin, http, helper = room
    state = tmp_path / "private"
    first = invoke(helper, state, "--url", origin, "connect")
    assert first.returncode == 2
    approval = json.loads(first.stdout)
    assert submit_approval(http, approval, "reject").status_code == 200

    refused = invoke(helper, state, "connect")
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["status"] == "error"
    assert "却下" in json.loads(refused.stdout)["message"]
    assert "approve_url" not in json.loads(refused.stdout)
    assert json.loads((state / "session.json").read_text())["pending"]["approve_url"] == approval["approve_url"]

    retried = invoke(helper, state, "connect", "--new-request")
    assert retried.returncode == 2
    assert json.loads(retried.stdout)["approve_url"] != approval["approve_url"]
