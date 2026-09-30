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

from .invite_words import WORDS

PASSWORD_METHOD = "scrypt:32768:8:3"
_DUMMY_HASH = generate_password_hash("no such account", method=PASSWORD_METHOD)


LAST_ADMIN_MESSAGE = (
    "마지막 관리자 계정은 탈퇴할 수 없어요. 서버에서 다른 관리자 계정을 만든 뒤 탈퇴해 주세요."
)


class AuthError(ValueError):
    def __init__(self, message="아이디나 비밀번호를 확인해 주세요.", status=401):
        super().__init__(message)
        self.status = status


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def invite_code(value: str) -> str | None:
    """대소문자와 구분자를 무시하고 세 단어 초대 코드를 표준형으로 바꾼다."""
    words = re.findall(r"[a-z]+", value.lower())
    if len(words) == 3 and all(word in WORDS for word in words):
        return "-".join(words)
    return None


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
                db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")

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
        # 단어 1,295개 세 개라 약 2.2e9 가지다. 가입 시도는 전체 60회/5분으로 막혀
        # (api.py auth-any) 7일짜리 코드를 맞힐 확률은 약 5e-5 이다.
        for _ in range(5):
            token = "-".join(secrets.choice(WORDS) for _ in range(3))
            try:
                with self.connect() as db:
                    db.execute(
                        "INSERT INTO invites VALUES (?, ?, NULL)",
                        (digest(token), self.clock() + ttl),
                    )
                return token
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("Could not allocate a unique invitation")

    @staticmethod
    def credentials(username, password):
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username):
            raise AuthError("앱 아이디는 영문, 숫자, 밑줄로 3~32자까지 입력해 주세요.", 400)
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise AuthError("앱 비밀번호는 12~128자로 입력해 주세요.", 400)
        return username.lower()

    def register(self, username, password, invite):
        username = self.credentials(username, password)
        if not isinstance(invite, str) or not invite.strip() or len(invite) > 128:
            raise AuthError("초대 코드가 올바르지 않거나 만료됐어요.", 403)
        # 예전 긴 토큰은 그대로, 세 단어 코드는 표준형으로 찾는다.
        raw = invite.strip()
        hashes = (digest(raw), digest(invite_code(raw) or raw))
        # Hash before the write transaction: password stretching must not hold
        # SQLite's exclusive writer lock while another friend is signing up.
        hashed = generate_password_hash(password, method=PASSWORD_METHOD)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE invites SET redeemed=? WHERE hash IN (?, ?) AND redeemed IS NULL"
                " AND expires>?",
                (self.clock(), *hashes, self.clock()),
            ).rowcount
            if not changed:
                raise AuthError("초대 코드가 올바르지 않거나 만료됐어요.", 403)
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
                raise AuthError("이미 사용 중인 앱 아이디예요.", 409) from exc
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
                    "관리자 계정이 아니에요."
                    if expected_role == "admin"
                    else "초대 회원 로그인 화면에서는 관리자 계정으로 로그인할 수 없어요.",
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
            raise AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.")
        public = self._public_user(user)
        return {
            "id": public["id"],
            "username": public["username"],
            "role": public["role"],
            "storage_id": user["storage_id"],
        }

    def check_password(self, user_id, password):
        """True if `password` is this account's password (re-entered before a destructive action)."""
        if not isinstance(password, str) or not 1 <= len(password) <= 128:
            return False
        with self.connect() as db:
            user = db.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
        valid = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
        return user is not None and valid

    def find_user(self, username):
        """The account as authenticate() returns it, by app ID; None if there is none."""
        if not isinstance(username, str):
            return None
        with self.connect() as db:
            user = db.execute(
                "SELECT * FROM users WHERE username=?", (username.lower(),)
            ).fetchone()
        return user and {**self._public_user(user), "storage_id": user["storage_id"]}

    def admin_count(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM users WHERE role='admin'").fetchone()[0]

    def delete_user(self, user_id, username, *, rate_limit_keys=()):
        """
        Remove the account, every session it holds and its own rate-limit buckets.

        The last admin is refused: invites come only from an admin, and the app
        would be left with nobody able to let a friend in. The operator can
        still create another with `python -m korail_bot.mobile admin` first.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            user = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if user is None:
                return False
            if (
                user["role"] == "admin"
                and db.execute("SELECT count(*) FROM users WHERE role='admin'").fetchone()[0] <= 1
            ):
                raise AuthError(LAST_ADMIN_MESSAGE, 409)
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM users WHERE id=?", (user_id,))
            keys = [*rate_limit_keys, "auth-user:" + username.lower()]
            db.executemany("DELETE FROM rate_limits WHERE key=?", [(digest(k),) for k in keys])
            return True

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
