"""SQLite identities and opaque sessions, independent of Telegram and Redis."""

import hashlib
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

PASSWORD_METHOD = "scrypt:32768:8:3"
_DUMMY_HASH = generate_password_hash("no such account", method=PASSWORD_METHOD)


class AuthError(ValueError):
    def __init__(self, message="인증 정보를 확인해주세요.", status=401):
        super().__init__(message)
        self.status = status


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


class IdentityStore:
    def __init__(self, path, *, clock=time.time, session_ttl=30 * 86400):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.clock = clock
        self.session_ttl = session_ttl
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, storage_id INTEGER UNIQUE NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member'
                );
                CREATE TABLE IF NOT EXISTS invites (
                    hash TEXT PRIMARY KEY, expires REAL NOT NULL, redeemed REAL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rate_limits (
                    key TEXT PRIMARY KEY, expires REAL NOT NULL, count INTEGER NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
            if "role" not in columns:
                db.execute(
                    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'"
                )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_invite(self, ttl=86400):
        if not 1 <= ttl <= 30 * 86400:
            raise ValueError("Invitation lifetime must be 1 second to 30 days")
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute(
                "INSERT INTO invites VALUES (?, ?, NULL)", (digest(token), self.clock() + ttl)
            )
        return token

    @staticmethod
    def credentials(username, password):
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username):
            raise AuthError("앱 아이디는 영문·숫자·밑줄 3~32자로 입력해주세요.", 400)
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise AuthError("앱 비밀번호는 12~128자로 입력해주세요.", 400)
        return username.lower()

    def register(self, username, password, invite):
        username = self.credentials(username, password)
        if not isinstance(invite, str) or not 16 <= len(invite) <= 128:
            raise AuthError("초대 코드가 유효하지 않거나 만료되었습니다.", 403)
        # Hash before the write transaction: password stretching must not hold
        # SQLite's exclusive writer lock while another friend is signing up.
        hashed = generate_password_hash(password, method=PASSWORD_METHOD)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE invites SET redeemed=? WHERE hash=? AND redeemed IS NULL AND expires>?",
                (self.clock(), digest(invite), self.clock()),
            ).rowcount
            if not changed:
                raise AuthError("초대 코드가 유효하지 않거나 만료되었습니다.", 403)
            user = {"id": "mobile_" + secrets.token_hex(16), "username": username, "role": "member"}
            try:
                db.execute(
                    "INSERT INTO users (id, username, password_hash, storage_id, role) VALUES (?, ?, ?, ?, 'member')",
                    (
                        user["id"],
                        username,
                        hashed,
                        -(2**51 + secrets.randbelow(2**50)),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthError("사용할 수 없는 앱 아이디입니다.", 409) from exc
            return self._session(db, user)

    def ensure_admin(self, username, password):
        username = self.credentials(username, password)
        hashed = generate_password_hash(password, method=PASSWORD_METHOD)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if existing:
                db.execute(
                    "UPDATE users SET password_hash=?, role='admin' WHERE id=?",
                    (hashed, existing["id"]),
                )
                return {"id": existing["id"], "username": username, "role": "admin"}
            user = {
                "id": "mobile_" + secrets.token_hex(16),
                "username": username,
                "role": "admin",
            }
            db.execute(
                "INSERT INTO users (id, username, password_hash, storage_id, role) VALUES (?, ?, ?, ?, 'admin')",
                (
                    user["id"],
                    username,
                    hashed,
                    -(2**51 + secrets.randbelow(2**50)),
                ),
            )
            return user

    def _public_user(self, user):
        role = dict(user).get("role", "member")
        if role not in {"admin", "member"}:
            role = "member"
        return {"id": user["id"], "username": user["username"], "role": role}

    def _session(self, db, user):
        token = secrets.token_urlsafe(32)
        expires = self.clock() + self.session_ttl
        db.execute("DELETE FROM sessions WHERE expires<=?", (self.clock(),))
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (digest(token), user["id"], expires))
        return {
            "token": token,
            "user": self._public_user(user),
            "expiresAt": timestamp(expires),
        }

    def login(self, username, password, expected_role=None):
        username = self.credentials(username, password)
        with self.connect() as db:
            user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            # Same expensive primitive for unknown accounts; never disclose
            # whether a particular invitation holder has registered.
            encoded = user["password_hash"] if user else _DUMMY_HASH
            valid = check_password_hash(encoded, password)
            if user is None or not valid:
                raise AuthError()
            role = self._public_user(user)["role"]
            if expected_role in {"admin", "member"} and role != expected_role:
                raise AuthError(
                    "관리자 계정이 아닙니다."
                    if expected_role == "admin"
                    else "선택받은 자 화면에서 관리자로 들어갈 수 없습니다.",
                    403,
                )
            return self._session(db, user)

    def authenticate(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise AuthError()
        with self.connect() as db:
            user = db.execute(
                "SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.hash=? AND sessions.expires>?",
                (digest(token), self.clock()),
            ).fetchone()
        if user is None:
            raise AuthError("앱 로그인이 만료되었습니다. 다시 로그인해주세요.")
        public = self._public_user(user)
        return {
            "id": public["id"],
            "username": public["username"],
            "role": public["role"],
            "storage_id": user["storage_id"],
        }

    def revoke(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE hash=?", (digest(token),))

    def allow(self, key, limit, window):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM rate_limits WHERE expires<=?", (self.clock(),))
            db.execute(
                "INSERT INTO rate_limits VALUES (?, ?, 1) ON CONFLICT(key) DO UPDATE SET count=count+1",
                (digest(key), self.clock() + window),
            )
            return (
                db.execute("SELECT count FROM rate_limits WHERE key=?", (digest(key),)).fetchone()[
                    0
                ]
                <= limit
            )
