"""One-time delivery of Muse credentials following pairing approval."""

import base64
import hashlib
import hmac
import os
import re
import secrets
import tempfile
import time
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .pairing_page import approval_page, confirmation_page

PAIRING_TTL = 10 * 60
MAX_OPEN_REQUESTS = 3
MAX_RECENT_REQUESTS = 12
MAX_CODE_ATTEMPTS = 5
REQUEST_RETENTION = 24 * 60 * 60
PUBLIC_PAIRING_POST = re.compile(r"/api/pairing/requests(?:/[^/]+/claim)?")
PAIRING_LINK_POST = re.compile(r"/pairing/(?:approve|reject)/[^/]+")

SCHEMA = """
    CREATE TABLE IF NOT EXISTS pairing_requests (
        id TEXT PRIMARY KEY,
        code TEXT,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'approved', 'rejected', 'invalidated', 'claimed', 'expired')),
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        claim_nonce BLOB
    );
    CREATE INDEX IF NOT EXISTS pairing_requests_created_at ON pairing_requests(created_at);
    CREATE TABLE IF NOT EXISTS pairing_approval_links (
        id TEXT PRIMARY KEY,
        pairing_id TEXT NOT NULL UNIQUE REFERENCES pairing_requests(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS issued_credentials (
        id TEXT PRIMARY KEY REFERENCES pairing_requests(id),
        token_hash TEXT NOT NULL UNIQUE,
        issued_at INTEGER NOT NULL,
        revoked_at INTEGER
    );
"""


class Code(BaseModel):
    code: str = Field(max_length=64)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def issued_muse(conn, token_hash):
    return conn.execute(
        """SELECT 1 FROM issued_credentials AS c
           JOIN pairing_requests AS p USING (id)
           WHERE c.token_hash = ? AND c.revoked_at IS NULL AND p.state = 'claimed'""",
        (token_hash,),
    ).fetchone() is not None


def private_key(data_dir, filename):
    path = data_dir / filename
    if not path.exists():
        # Publish a complete private key even if two processes start together.
        with tempfile.NamedTemporaryFile(dir=data_dir) as temporary:
            temporary.write(secrets.token_bytes(32))
            temporary.flush()
            try:
                os.link(temporary.name, path)
            except FileExistsError:
                pass
    key = path.read_bytes()
    if len(key) != 32 or path.stat().st_mode & 0o077:
        raise RuntimeError(f"Invalid or unprotected {filename}")
    return key


def derive_token(key, nonce):
    # Retain the nonce only until delivery or cancellation. SQLite stores the
    # credential hash, and the separate server key allows delivery after a restart.
    material = hmac.digest(key, b"retri:muse:pairing:v1:" + nonce, "sha256")
    return base64.urlsafe_b64encode(material).rstrip(b"=").decode("ascii")


def approval_key(data_dir):
    secret = os.environ.get("CHAT_PAIRING_SECRET")
    if not secret:
        return private_key(data_dir, "pairing-approval.key")
    if len(secret) < 32:
        raise RuntimeError("CHAT_PAIRING_SECRET must contain at least 32 characters")
    return secret.encode()


def sign_approval(key, pairing_id, approval_id, expires_at):
    message = f"retri:muse:approve-link:v1:{pairing_id}:{approval_id}:{expires_at}"
    signature = hmac.digest(key, message.encode("ascii"), "sha256")
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


async def submitted_signature(request):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        try:
            body = await request.json()
        except ValueError:
            return None
        return body.get("t") if isinstance(body, dict) else None
    if content_type == "application/x-www-form-urlencoded":
        async with request.form() as form:
            return form.get("t")
    return None


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def register_pairing(app, db, require_owner, changed, data_dir, origin):
    key = private_key(data_dir, "pairing.key")
    link_key = approval_key(data_dir)
    with db() as conn:
        conn.executescript(SCHEMA)
    router = APIRouter(prefix="/api/pairing")

    @router.post("/requests", status_code=201)
    async def create_request():
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            recent = conn.execute(
                "SELECT COUNT(*) FROM pairing_requests WHERE created_at > ?",
                (now - PAIRING_TTL,),
            ).fetchone()[0]
            open_codes = {
                row["code"]
                for row in conn.execute(
                    """SELECT code FROM pairing_requests
                       WHERE state IN ('pending', 'approved') AND expires_at > ?""",
                    (now,),
                )
            }
            if recent >= MAX_RECENT_REQUESTS or len(open_codes) >= MAX_OPEN_REQUESTS:
                raise HTTPException(429, "pairing_limit")
            conn.execute(
                """UPDATE pairing_requests SET state = 'expired', code = NULL, claim_nonce = NULL
                   WHERE expires_at <= ? AND state IN ('pending', 'approved')""",
                (now,),
            )
            conn.execute(
                """DELETE FROM pairing_requests
                   WHERE expires_at <= ? AND id NOT IN (SELECT id FROM issued_credentials)""",
                (now - REQUEST_RETENTION,),
            )
            code = f"{secrets.randbelow(1_000_000):06d}"
            while code in open_codes:
                code = f"{secrets.randbelow(1_000_000):06d}"
            pairing_id = secrets.token_urlsafe(32)
            expires_at = now + PAIRING_TTL
            conn.execute(
                "INSERT INTO pairing_requests (id, code, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (pairing_id, code, now, expires_at),
            )
            # The URL can display the code safely because its identifier cannot claim a credential.
            approval_id = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO pairing_approval_links (id, pairing_id) VALUES (?, ?)",
                (approval_id, pairing_id),
            )
        signature = sign_approval(link_key, pairing_id, approval_id, expires_at)
        return {
            "pairing_id": pairing_id,
            "code": code,
            "expires_at": timestamp(expires_at),
            "approve_url": f"{origin}/pairing/approve/{approval_id}?t={signature}",
        }

    @router.post("/requests/{pairing_id}/claim")
    async def claim(pairing_id: str, body: Code):
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT p.*, c.token_hash FROM pairing_requests AS p
                   LEFT JOIN issued_credentials AS c USING (id) WHERE p.id = ?""",
                (pairing_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "not_found")
            terminal = {
                "claimed": (409, "claimed"),
                "rejected": (403, "rejected"),
                "invalidated": (429, "too_many_attempts"),
                "expired": (410, "expired"),
            }.get(row["state"])
            if terminal:
                raise HTTPException(*terminal)
            if row["expires_at"] <= int(time.time()):
                raise HTTPException(410, "expired")
            if not secrets.compare_digest(body.code.encode(), row["code"].encode()):
                attempts = row["failed_attempts"] + 1
                if attempts >= MAX_CODE_ATTEMPTS:
                    conn.execute(
                        """UPDATE pairing_requests SET state = 'invalidated', failed_attempts = ?,
                           code = NULL, claim_nonce = NULL WHERE id = ?""",
                        (attempts, pairing_id),
                    )
                else:
                    conn.execute(
                        "UPDATE pairing_requests SET failed_attempts = ? WHERE id = ?",
                        (attempts, pairing_id),
                    )
            else:
                if row["state"] == "pending":
                    raise HTTPException(425, "pending")
                token = derive_token(key, row["claim_nonce"])
                if not secrets.compare_digest(digest(token), row["token_hash"]):
                    raise HTTPException(503, "pairing_unavailable")
                conn.execute(
                    """UPDATE pairing_requests SET state = 'claimed', code = NULL,
                       claim_nonce = NULL WHERE id = ?""",
                    (pairing_id,),
                )
                attempts = 0
        # The write transaction has committed before any credential is returned.
        if attempts >= MAX_CODE_ATTEMPTS:
            raise HTTPException(429, "too_many_attempts")
        if attempts:
            raise HTTPException(401, "wrong_code")
        return {"author": "muse", "token": token}

    @router.get("/requests")
    async def list_requests(request: Request):
        require_owner(request)
        with db() as conn:
            rows = conn.execute(
                """SELECT id, code, created_at, expires_at FROM pairing_requests
                   WHERE state = 'pending' AND expires_at > ? ORDER BY created_at, id""",
                (int(time.time()),),
            ).fetchall()
        return {
            "requests": [
                {
                    "pairing_id": row["id"],
                    "code": row["code"],
                    "created_at": timestamp(row["created_at"]),
                    "expires_at": timestamp(row["expires_at"]),
                }
                for row in rows
            ]
        }

    def pending_request(conn, pairing_id, now):
        row = conn.execute(
            "SELECT code, state, expires_at FROM pairing_requests WHERE id = ?",
            (pairing_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "申請が見つかりません。")
        if row["state"] != "pending" or row["expires_at"] <= now:
            raise HTTPException(409, "この申請は期限切れか、すでに処理済みです。")
        return row

    def approve_request(conn, pairing_id, now):
        nonce = secrets.token_bytes(32)
        conn.execute(
            "UPDATE pairing_requests SET state = 'approved', claim_nonce = ? WHERE id = ?",
            (nonce, pairing_id),
        )
        conn.execute(
            "INSERT INTO issued_credentials (id, token_hash, issued_at) VALUES (?, ?, ?)",
            (pairing_id, digest(derive_token(key, nonce)), now),
        )

    def reject_request(conn, pairing_id):
        conn.execute(
            "UPDATE pairing_requests SET state = 'rejected', code = NULL WHERE id = ?",
            (pairing_id,),
        )

    @router.post("/requests/{pairing_id}/approve")
    async def approve(pairing_id: str, body: Code, request: Request):
        require_owner(request)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            row = pending_request(conn, pairing_id, now)
            if not secrets.compare_digest(body.code.encode(), row["code"].encode()):
                raise HTTPException(422, "入力した確認コードが申請と一致しません。")
            approve_request(conn, pairing_id, now)
        return {"ok": True}

    @router.post("/requests/{pairing_id}/reject")
    async def reject(pairing_id: str, request: Request):
        require_owner(request)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending_request(conn, pairing_id, int(time.time()))
            reject_request(conn, pairing_id)
        return {"ok": True}

    @router.get("/credentials")
    async def list_credentials(request: Request):
        require_owner(request)
        with db() as conn:
            rows = conn.execute(
                """SELECT c.id, c.issued_at,
                          CASE
                              WHEN c.revoked_at IS NOT NULL THEN 'revoked'
                              WHEN p.state = 'claimed' THEN 'active'
                              WHEN p.state = 'invalidated' THEN 'invalidated'
                              WHEN p.state = 'expired' OR p.expires_at <= ? THEN 'expired'
                              ELSE 'unclaimed'
                          END AS status
                   FROM issued_credentials AS c JOIN pairing_requests AS p USING (id)
                   ORDER BY c.issued_at DESC, c.id""",
                (int(time.time()),),
            ).fetchall()
        return {
            "credentials": [
                {
                    "credential_id": row["id"],
                    "author": "muse",
                    "issued_at": timestamp(row["issued_at"]),
                    "status": row["status"],
                }
                for row in rows
            ]
        }

    @router.post("/credentials/{credential_id}/revoke")
    async def revoke(credential_id: str, request: Request):
        require_owner(request)
        with db() as conn:
            result = conn.execute(
                """UPDATE issued_credentials SET revoked_at = COALESCE(revoked_at, ?)
                   WHERE id = ?""",
                (int(time.time()), credential_id),
            )
            if not result.rowcount:
                raise HTTPException(404, "発行済み資格が見つかりません。")
            conn.execute(
                """UPDATE pairing_requests SET state = 'rejected', code = NULL, claim_nonce = NULL
                   WHERE id = ? AND state = 'approved'""",
                (credential_id,),
            )
        async with changed:
            changed.notify_all()
        return {"ok": True}

    def signed_request(conn, approval_id, signature, now):
        row = conn.execute(
            """SELECT p.id, p.code, p.created_at, p.expires_at, p.state
               FROM pairing_approval_links AS link
               JOIN pairing_requests AS p ON p.id = link.pairing_id
               WHERE link.id = ?""",
            (approval_id,),
        ).fetchone()
        if row is None or not isinstance(signature, str) or not signature.isascii():
            raise HTTPException(403, "この承認リンクは無効です。")
        expected = sign_approval(link_key, row["id"], approval_id, row["expires_at"])
        if not secrets.compare_digest(expected, signature):
            raise HTTPException(403, "この承認リンクは無効です。")
        if row["state"] not in ("pending", "expired"):
            raise HTTPException(409, "この申請は処理済みです。")
        if row["state"] == "expired" or row["expires_at"] <= now:
            raise HTTPException(410, "この承認リンクは期限切れです。")
        return row

    @app.get("/pairing/approve/{approval_id}")
    async def review(approval_id: str, request: Request):
        signature = request.query_params.get("t")
        try:
            with db() as conn:
                row = signed_request(conn, approval_id, signature, int(time.time()))
        except HTTPException as error:
            return approval_page(error.detail, status_code=error.status_code)
        return confirmation_page(
            approval_id,
            signature,
            code=row["code"],
            created_at=timestamp(row["created_at"]),
            expires_at=timestamp(row["expires_at"]),
        )

    async def process_link(approval_id, request, *, reject=False):
        signature = await submitted_signature(request)
        try:
            with db() as conn:
                # Recheck the signature and pending state under the same write lock as Owner actions.
                conn.execute("BEGIN IMMEDIATE")
                now = int(time.time())
                row = signed_request(conn, approval_id, signature, now)
                if reject:
                    reject_request(conn, row["id"])
                else:
                    approve_request(conn, row["id"], now)
        except HTTPException as error:
            return approval_page(error.detail, status_code=error.status_code)
        if reject:
            return approval_page("却下しました。")
        return approval_page(
            "承認しました。",
            "<p>Muse に『承認した』と伝えてください。</p>",
        )

    @app.post("/pairing/approve/{approval_id}")
    async def approve_link(approval_id: str, request: Request):
        return await process_link(approval_id, request)

    @app.post("/pairing/reject/{approval_id}")
    async def reject_link(approval_id: str, request: Request):
        return await process_link(approval_id, request, reject=True)

    app.include_router(router)
