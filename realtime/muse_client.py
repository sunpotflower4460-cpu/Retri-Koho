#!/usr/bin/env python3
"""Connect a Muse VM to Retri-Koho using the pairing and chat APIs."""

import argparse
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

PAIRING_PATH = "/api/pairing/requests"
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,512}")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{43}")
CODE = re.compile(r"[0-9]{6}")


class ClientError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = build_opener(ProxyHandler({}), NoRedirects())


def request_json(origin, path, *, token=None, body=None, form=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(origin + path, data=data, headers=headers)
    try:
        with OPENER.open(request, timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        with error:
            return error.code, {}
    except (URLError, OSError, HTTPException):
        raise ClientError("会議室に接続できません。Muse VM の tailnet 接続を確認してください。") from None
    except ValueError:
        raise ClientError("会議室の応答を読み込めません。") from None


def canonical_origin(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ClientError("会議室の HTTPS オリジンを --url に指定してください。") from None
    if (
        not host or any(character.isspace() for character in url)
        or parsed.scheme not in ("http", "https") or parsed.username is not None
        or parsed.path not in ("", "/") or parsed.query or parsed.fragment
        or (parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"))
    ):
        raise ClientError("会議室の HTTPS オリジンを --url に指定してください。")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        netloc += f":{port}"
    return f"{parsed.scheme}://{netloc}"


def private_mode(info, expected_type):
    return (
        expected_type(info.st_mode) and info.st_uid == os.getuid()
        and not info.st_mode & 0o077
    )


class SessionStore:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not private_mode(directory.lstat(), stat.S_ISDIR):
            raise ClientError("Muse の保存先は自分だけが使えるディレクトリにしてください（権限700）。")
        self.path = directory / "session.json"

    @contextmanager
    def locked(self):
        fd = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            if not private_mode(os.fstat(lock.fileno()), stat.S_ISREG):
                raise ClientError("Muse の接続情報を保管するファイルの権限を確認してください。")
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, encoding="utf-8") as source:
            if not private_mode(os.fstat(source.fileno()), stat.S_ISREG):
                raise ClientError("Muse の接続情報は自分だけが読めるファイルに保存してください（権限600）。")
            try:
                state = json.load(source)
            except ValueError:
                raise ClientError("Muse の保存済み接続情報を読み込めません。") from None
        if not isinstance(state, dict) or not isinstance(state.get("origin"), str):
            raise ClientError("Muse の保存済み接続情報を読み込めません。")
        return state

    def save(self, state):
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.directory, delete=False
        ) as temporary:
            path = Path(temporary.name)
            try:
                json.dump(state, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
                os.replace(path, self.path)
            finally:
                path.unlink(missing_ok=True)


def valid_string(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def pairing_details(origin, response):
    if isinstance(response, dict):
        identifier = response.get("pairing_id")
        code = response.get("code")
        expiry = response.get("expires_at")
        url = response.get("approve_url")
        if (
            valid_string(identifier, REQUEST_ID) and valid_string(code, CODE)
            and isinstance(expiry, str) and isinstance(url, str)
            and url.startswith(origin + "/pairing/approve/")
        ):
            path, separator, signature = url.removeprefix(origin + "/pairing/approve/").partition("?t=")
            if (
                separator and valid_string(path, REQUEST_ID) and path != identifier
                and valid_string(signature, REQUEST_ID)
            ):
                return {"pairing_id": identifier, "code": code, "expires_at": expiry, "approve_url": url}
    raise ClientError("会議室から有効なペアリング申請を受け取れませんでした。")


def approval_required(pending):
    return {
        "status": "approval_required",
        "approve_url": pending["approve_url"],
        "expires_at": pending["expires_at"],
    }


def connect_once(store, requested_url, new_request):
    # Keep the state lock through each claim and save so only one local process can claim.
    with store.locked():
        state = store.load()
        saved_origin = state.get("origin")
        url = requested_url or saved_origin or os.environ.get("CHAT_URL")
        if not url:
            raise ClientError("初回は --url に会議室の URL を指定してください。")
        origin = canonical_origin(url)
        if saved_origin and origin != saved_origin:
            raise ClientError("保存済みの会議室と URL が異なります。別の会議室には --state-dir を指定してください。")

        token = state.get("token")
        if token is not None:
            if not valid_string(token, TOKEN):
                raise ClientError("保存済みのMuse用資格を読み込めません。")
            status, me = request_json(origin, "/api/me", token=token)
            if status == 200:
                if not isinstance(me, dict) or me.get("author") != "muse":
                    raise ClientError("保存済みの接続資格はMuse用ではありません。")
                return (origin, token), None
            if status != 401:
                raise ClientError(f"Muse の認証状態を確認できませんでした（HTTP {status}）。")
            # A revoked credential requires another explicit approval.
            store.save({"origin": origin})

        pending = state.get("pending")
        if pending is not None:
            pending = pairing_details(origin, pending)
            path = f"{PAIRING_PATH}/{pending['pairing_id']}/claim"
            status, claimed = request_json(origin, path, body={"code": pending["code"]})
            if status == 200:
                token = claimed.get("token") if isinstance(claimed, dict) else None
                if not valid_string(token, TOKEN) or claimed.get("author") != "muse":
                    raise ClientError("会議室からMuse用資格を受け取れませんでした。")
                store.save({"origin": origin, "token": token})
                return (origin, token), None
            if status == 425:
                return None, approval_required(pending)
            if status in (401, 403, 409, 429):
                if not new_request:
                    if status == 409:
                        raise ClientError("申請は受領済みです。Owner が該当資格を取り消した後、connect --new-request を実行してください。")
                    raise ClientError("申請が却下または無効になりました。再申請する場合は connect --new-request を実行してください。")
            elif status not in (404, 410):
                raise ClientError(f"ペアリングの状態を確認できませんでした（HTTP {status}）。")

        status, result = request_json(origin, PAIRING_PATH, body={})
        if status == 429:
            raise ClientError("会議室の申請上限に達しました。時間を空けてから接続してください。")
        if status != 201:
            raise ClientError(f"ペアリング申請を作れませんでした（HTTP {status}）。")
        pending = pairing_details(origin, result)
        store.save({"origin": origin, "pending": pending})
        return None, approval_required(pending)


def emit(result):
    print(json.dumps(result, ensure_ascii=False), flush=True)


def connect(store, url, wait, new_request):
    deadline = time.monotonic() + wait
    last_link = None
    while True:
        connection, approval = connect_once(store, url, new_request)
        if connection:
            return connection
        if approval["approve_url"] != last_link:
            emit(approval)
            last_link = approval["approve_url"]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(2, remaining))


def bounded_int(minimum, maximum):
    def parse(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"{minimum}–{maximum} の範囲で指定してください。")
        return number
    return parse


def main():
    default_state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="会議室のHTTPSオリジン。初回の接続時に指定します。")
    parser.add_argument(
        "--state-dir", type=Path,
        default=os.environ.get("RETRI_MUSE_STATE_DIR") or default_state / "retri-koho/muse",
        help="このVM内の永続ストレージ上にある非公開の保存先。",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("connect", help="保存済み資格で接続し、必要な場合は承認リンクを発行します。")
    login.add_argument("--new-request", action="store_true", help="却下・無効・受領済みの申請から再申請します。")
    history = commands.add_parser("history", help="会議室の最近の発言を読みます。")
    history.add_argument("--limit", type=bounded_int(1, 100), default=20)
    post = commands.add_parser("post", help="標準入力で受け取った文章を投稿します。")
    post.add_argument("--client-id", type=UUID, help="同じ投稿を再試行するときに指定するUUID。")
    for command in (login, history, post):
        command.add_argument("--wait", type=bounded_int(0, 600), default=0, help="承認を待つ最長秒数。")
    args = parser.parse_args()

    try:
        store = SessionStore(args.state_dir.expanduser())
        connection = connect(store, args.url, args.wait, getattr(args, "new_request", False))
        if connection is None:
            return 2
        origin, token = connection
        if args.command == "connect":
            emit({"status": "connected", "author": "muse"})
            return 0
        if args.command == "history":
            status, result = request_json(origin, f"/api/messages?limit={args.limit}", token=token)
        else:
            body = sys.stdin.read().strip()
            if not 1 <= len(body) <= 4000:
                raise ClientError("投稿する本文を標準入力で指定してください（1–4,000文字）。")
            form = {"body": body, "client_id": str(args.client_id or uuid4())}
            status, result = request_json(origin, "/api/messages", token=token, form=form)
        if status == 401:
            raise ClientError("Muse の認証が無効になりました。connect で接続し直してください。")
        if status not in (200, 201):
            raise ClientError(f"会議室への操作に失敗しました（HTTP {status}）。")
        emit(result)
        return 0
    except ClientError as error:
        emit({"status": "error", "message": str(error)})
    except (OSError, UnicodeError):
        emit({"status": "error", "message": "このVMの接続情報を読み書きできませんでした。"})
    return 1


if __name__ == "__main__":
    sys.exit(main())
