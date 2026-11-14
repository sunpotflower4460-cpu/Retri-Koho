"""Private, persistent Muse / dots room. Run with one Uvicorn worker."""

import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

HERE = Path(__file__).resolve().parent
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_TOTAL_BYTES + 128 * 1024
MAX_FILES = 5
SESSION_SECONDS = 7 * 24 * 60 * 60
COOKIE = "retri_session"
LABELS = {"owner": "あなた", "muse": "Muse", "dots": "dots"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    origin: str
    tokens: dict[str, str]

    @classmethod
    def from_env(cls):
        origin = os.environ.get("CHAT_ORIGIN", "http://localhost:8000").rstrip("/")
        parts = urlsplit(origin)
        if (
            parts.scheme not in ("http", "https")
            or not parts.netloc
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username
        ):
            raise RuntimeError(
                "CHAT_ORIGIN must be an http(s) origin, e.g. https://chat.example.com"
            )
        tokens = {
            name: os.environ.get(f"CHAT_{name.upper()}_TOKEN", "") for name in LABELS
        }
        if (
            any(len(value) < 32 for value in tokens.values())
            or len(set(tokens.values())) != 3
        ):
            raise RuntimeError(
                "Set distinct CHAT_OWNER_TOKEN, CHAT_MUSE_TOKEN, CHAT_DOTS_TOKEN (32+ characters each)"
            )
        return cls(Path(os.environ.get("CHAT_DATA_DIR", HERE / "data")), origin, tokens)


class BodyTooLarge(Exception):
    pass


class BodyLimit:
    """Bound multipart bodies, including chunked requests, before disk can fill up."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Invalid Content-Length"}, 400)(
                scope, receive, send
            )
        if length > MAX_REQUEST_BYTES:
            return await JSONResponse({"detail": "添付の合計は20 MBまでです。"}, 413)(
                scope, receive, send
            )
        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > MAX_REQUEST_BYTES:
                raise BodyTooLarge()
            return message

        await self.app(scope, limited_receive, send)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def image_type(header):
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def create_app(settings=None):
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    uploads = settings.data_dir / "uploads"
    uploads.mkdir(exist_ok=True)
    database = settings.data_dir / "chat.sqlite3"

    @contextmanager
    def db():
        connection = sqlite3.connect(database, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                client_id TEXT NOT NULL,
                UNIQUE(author, client_id)
            );
            CREATE TABLE IF NOT EXISTS attachments (
                id TEXT PRIMARY KEY,
                message_id INTEGER NOT NULL REFERENCES messages(id),
                name TEXT NOT NULL,
                size INTEGER NOT NULL,
                media_type TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS attachments_message ON attachments(message_id);
            CREATE TABLE IF NOT EXISTS sessions (
                hash TEXT PRIMARY KEY,
                author TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
        """)

    app = FastAPI(
        title="Re:trip · Muse / dots room",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodyLimit)
    changed = asyncio.Condition()
    # A small shared rate limit also prevents unbounded per-IP memory use.
    login_attempts = []

    @app.exception_handler(BodyTooLarge)
    async def body_too_large(request, exc):
        return JSONResponse({"detail": "添付の合計は20 MBまでです。"}, 413)

    @app.middleware("http")
    async def protect(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != settings.origin:
                return JSONResponse({"detail": "別のサイトからは送信できません。"}, 403)
            if (
                not request.headers.get("authorization", "").startswith("Bearer ")
                and request.headers.get("x-chat-request") != "1"
            ):
                return JSONResponse({"detail": "X-Chat-Request header required"}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def identity(request):
        authorization = request.headers.get("authorization", "")
        if authorization:
            if authorization.startswith("Bearer "):
                value = authorization[7:]
                for author, token in settings.tokens.items():
                    if secrets.compare_digest(value.encode(), token.encode()):
                        return author
            raise HTTPException(401, "接続キーを確認してください。")
        session = request.cookies.get(COOKIE, "")
        if session:
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE hash = ? AND expires_at > ?",
                    (digest(session), time.time()),
                ).fetchone()
            if row and secrets.compare_digest(
                row["token_hash"], digest(settings.tokens.get(row["author"], ""))
            ):
                return row["author"]
        raise HTTPException(401, "接続キーで入室してください。")

    def message_rows(conn, rows):
        result = []
        for row in rows:
            item = dict(row)
            item.pop("client_id", None)
            item["attachments"] = [
                dict(a)
                for a in conn.execute(
                    "SELECT id, name, size, media_type FROM attachments WHERE message_id = ? ORDER BY rowid",
                    (row["id"],),
                )
            ]
            result.append(item)
        return result

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    class Login(BaseModel):
        token: str = Field(min_length=1, max_length=512)

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        now = time.time()
        login_attempts[:] = [t for t in login_attempts if t > now - 60]
        if len(login_attempts) >= 30:
            raise HTTPException(
                429, "少し待ってから再度お試しください。", headers={"Retry-After": "60"}
            )
        login_attempts.append(now)
        author = next(
            (
                name
                for name, token in settings.tokens.items()
                if secrets.compare_digest(body.token.encode(), token.encode())
            ),
            None,
        )
        if not author:
            raise HTTPException(401, "接続キーを確認してください。")
        value = secrets.token_urlsafe(32)
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            old = request.cookies.get(COOKIE)
            if old:
                conn.execute("DELETE FROM sessions WHERE hash = ?", (digest(old),))
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (
                    digest(value),
                    author,
                    digest(settings.tokens[author]),
                    now + SESSION_SECONDS,
                ),
            )
        response = JSONResponse({"author": author, "label": LABELS[author]})
        response.set_cookie(
            COOKIE,
            value,
            httponly=True,
            secure=settings.origin.startswith("https:"),
            samesite="strict",
            max_age=SESSION_SECONDS,
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        with db() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE hash = ?",
                (digest(request.cookies.get(COOKIE, "")),),
            )
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/me")
    async def me(request: Request):
        author = identity(request)
        return {
            "author": author,
            "label": LABELS[author],
            "limits": {
                "files": MAX_FILES,
                "file_bytes": MAX_FILE_BYTES,
                "total_bytes": MAX_TOTAL_BYTES,
            },
        }

    @app.get("/api/messages")
    async def history(request: Request, before: int | None = None, limit: int = 50):
        identity(request)
        if not 1 <= limit <= 100 or (
            before is not None and not 1 <= before <= 9223372036854775807
        ):
            raise HTTPException(422, "Invalid cursor or limit")
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?",
                (before if before is not None else 9223372036854775807, limit + 1),
            ).fetchall()
            messages = message_rows(conn, reversed(rows[:limit]))
        return {"messages": messages, "has_more": len(rows) > limit}

    @app.post("/api/messages")
    async def publish(request: Request):
        author = identity(request)
        # Parsing limits apply before a message or permanent upload is written.
        async with request.form(
            max_files=MAX_FILES, max_fields=2, max_part_size=16 * 1024
        ) as form:
            body = form.get("body", "")
            client_id = form.get("client_id", "")
            files = form.getlist("files")
            if not isinstance(body, str) or len(body.strip()) > 4000:
                raise HTTPException(422, "本文は4,000文字までです。")
            body = body.strip()
            try:
                client_id = str(UUID(client_id))
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(422, "client_id must be a UUID")
            if any(not isinstance(file, UploadFile) for file in files):
                raise HTTPException(422, "Invalid attachment")
            if not body and not files:
                raise HTTPException(422, "本文またはファイルを追加してください。")
            with db() as conn:
                existing = conn.execute(
                    "SELECT * FROM messages WHERE author = ? AND client_id = ?",
                    (author, client_id),
                ).fetchone()
                if existing:
                    return message_rows(conn, [existing])[0]
            saved = []
            total = 0
            committed = False
            try:
                for file in files:
                    file_id = uuid4().hex
                    name = (
                        re.sub(
                            r"[\x00-\x1f\x7f]",
                            "",
                            (file.filename or "file").replace("\\", "/").split("/")[-1],
                        )[:180]
                        or "file"
                    )
                    record = {
                        "id": file_id,
                        "name": name,
                        "size": 0,
                        "media_type": "application/octet-stream",
                    }
                    saved.append(record)
                    with (uploads / file_id).open("xb") as target:
                        while chunk := await file.read(64 * 1024):
                            if record["size"] == 0:
                                record["media_type"] = image_type(chunk)
                            record["size"] += len(chunk)
                            total += len(chunk)
                            if (
                                record["size"] > MAX_FILE_BYTES
                                or total > MAX_TOTAL_BYTES
                            ):
                                raise HTTPException(
                                    413, "1ファイル10 MB、合計20 MBまでです。"
                                )
                            target.write(chunk)
                with db() as conn:
                    # Another simultaneous retry may already have committed this client_id.
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO messages(author, body, created_at, client_id) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)",
                        (author, body, client_id),
                    )
                    row = conn.execute(
                        "SELECT * FROM messages WHERE author = ? AND client_id = ?",
                        (author, client_id),
                    ).fetchone()
                    if cursor.rowcount:
                        conn.executemany(
                            "INSERT INTO attachments VALUES (?, ?, ?, ?, ?)",
                            [
                                (
                                    f["id"],
                                    row["id"],
                                    f["name"],
                                    f["size"],
                                    f["media_type"],
                                )
                                for f in saved
                            ],
                        )
                        result = message_rows(conn, [row])[0]
                    else:
                        return message_rows(conn, [row])[0]
                committed = True
            finally:
                if not committed:
                    for file in saved:
                        (uploads / file["id"]).unlink(missing_ok=True)
        async with changed:
            changed.notify_all()
        return JSONResponse(result, 201)

    @app.get("/api/attachments/{file_id}")
    async def attachment(file_id: str, request: Request, download: bool = False):
        identity(request)
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (file_id,)
            ).fetchone()
        if not row or not (uploads / file_id).is_file():
            raise HTTPException(404, "ファイルが見つかりません。")
        inline = row["media_type"].startswith("image/") and not download
        return FileResponse(
            uploads / file_id,
            filename=row["name"],
            media_type=row["media_type"],
            content_disposition_type="inline" if inline else "attachment",
        )

    @app.get("/api/events")
    async def events(request: Request, after: int = 0):
        identity(request)
        try:
            # EventSource sets Last-Event-ID on reconnect; it takes precedence over the initial URL.
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
            if (
                not 0 <= after <= 9223372036854775807
                or not 0 <= cursor <= 9223372036854775807
            ):
                raise ValueError()
        except ValueError:
            raise HTTPException(422, "Invalid event cursor")

        async def stream():
            nonlocal cursor
            yield "retry: 1500\n\n"
            while not await request.is_disconnected():
                try:
                    identity(request)
                except HTTPException:
                    yield "event: session-expired\ndata: {}\n\n"
                    return
                async with changed:
                    with db() as conn:
                        batch = message_rows(
                            conn,
                            conn.execute(
                                "SELECT * FROM messages WHERE id > ? ORDER BY id LIMIT 100",
                                (cursor,),
                            ).fetchall(),
                        )
                    if not batch:
                        try:
                            await asyncio.wait_for(changed.wait(), timeout=1)
                        except TimeoutError:
                            pass
                if not batch:
                    yield ": keep-alive\n\n"
                for item in batch:
                    cursor = item["id"]
                    yield f"id: {cursor}\nevent: message\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )

    app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="ui")
    return app
