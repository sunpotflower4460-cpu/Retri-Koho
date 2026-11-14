"""Owner-approved, one-time delivery of Muse credentials."""

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

PAIRING_TTL = 10 * 60
MAX_OPEN_REQUESTS = 3
MAX_RECENT_REQUESTS = 12
MAX_CODE_ATTEMPTS = 5
REQUEST_RETENTION = 24 * 60 * 60
PUBLIC_PAIRING_POST = re.compile(r"/api/pairing/requests(?:/[^/]+/claim)?")

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


def pairing_key(data_dir):
    path = data_dir / "pairing.key"
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
        raise RuntimeError("Invalid or unprotected pairing.key")
    return key


def derive_token(key, nonce):
    # Retain the nonce only until delivery or cancellation. SQLite stores the
    # credential hash, and the separate server key allows delivery after a restart.
    material = hmac.digest(key, b"retri:muse:pairing:v1:" + nonce, "sha256")
    return base64.urlsafe_b64encode(material).rstrip(b"=").decode("ascii")


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def register_pairing(app, db, require_owner, changed, data_dir):
    key = pairing_key(data_dir)
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
        return {"pairing_id": pairing_id, "code": code, "expires_at": timestamp(expires_at)}

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

    @router.post("/requests/{pairing_id}/approve")
    async def approve(pairing_id: str, body: Code, request: Request):
        require_owner(request)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            row = pending_request(conn, pairing_id, now)
            if not secrets.compare_digest(body.code.encode(), row["code"].encode()):
                raise HTTPException(422, "入力した確認コードが申請と一致しません。")
            nonce = secrets.token_bytes(32)
            conn.execute(
                "UPDATE pairing_requests SET state = 'approved', claim_nonce = ? WHERE id = ?",
                (nonce, pairing_id),
            )
            conn.execute(
                "INSERT INTO issued_credentials (id, token_hash, issued_at) VALUES (?, ?, ?)",
                (pairing_id, digest(derive_token(key, nonce)), now),
            )
        return {"ok": True}

    @router.post("/requests/{pairing_id}/reject")
    async def reject(pairing_id: str, request: Request):
        require_owner(request)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending_request(conn, pairing_id, int(time.time()))
            conn.execute(
                "UPDATE pairing_requests SET state = 'rejected', code = NULL WHERE id = ?",
                (pairing_id,),
            )
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

    app.include_router(router)
