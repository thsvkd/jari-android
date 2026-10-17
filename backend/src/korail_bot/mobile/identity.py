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


LAST_ADMIN_MESSAGE = (
    "마지막 관리자 계정은 탈퇴할 수 없어요. 서버에서 다른 관리자 계정을 만든 뒤 탈퇴해 주세요."
)


class AuthError(ValueError):
    def __init__(self, message="아이디나 비밀번호를 확인해 주세요.", status=401, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


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
                    role TEXT NOT NULL DEFAULT 'member',
                    status TEXT NOT NULL DEFAULT 'approved',
                    application_note TEXT,
                    created REAL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires REAL NOT NULL, authenticated_at REAL,
                    via_google INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS rate_limits (
                    key TEXT PRIMARY KEY, expires REAL NOT NULL, count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS google_identities (
                    subject TEXT PRIMARY KEY,
                    user_id TEXT UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS google_flows (
                    id TEXT PRIMARY KEY, proof_hash TEXT NOT NULL,
                    state_hash TEXT UNIQUE NOT NULL, nonce TEXT NOT NULL,
                    code_verifier TEXT NOT NULL,
                    verification_hash TEXT NOT NULL,
                    initiator_label TEXT NOT NULL, initiator_ip TEXT NOT NULL,
                    mode TEXT NOT NULL, user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
                    session_hash TEXT, expires REAL NOT NULL,
                    callback_used INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending', subject TEXT, error TEXT,
                    confirmation_hash TEXT, confirmation_cookie_hash TEXT,
                    confirmation_cookie_name TEXT, confirmation_attempts INTEGER NOT NULL DEFAULT 0,
                    delivered_session_hash TEXT, delivered_link INTEGER NOT NULL DEFAULT 0,
                    app_return INTEGER NOT NULL DEFAULT 0, handoff_hash TEXT
                );
                -- Agent connections (oauth.py). Secrets are kept as digests only, and deleting
                -- a user cascades to its connections, their codes and their tokens.
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect_uris TEXT NOT NULL,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_grants (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, scope TEXT NOT NULL,
                    created REAL NOT NULL, last_used REAL, expires REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    hash TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
                    client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
                    code_challenge TEXT NOT NULL, expires REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                -- The device-code flow's requests, replaced by signing in on the browser page.
                DROP TABLE IF EXISTS oauth_requests;
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    hash TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL, resource TEXT NOT NULL, expires REAL NOT NULL,
                    rotated INTEGER NOT NULL DEFAULT 0
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
            if "role" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
            if "status" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'")
            if "application_note" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN application_note TEXT")
            if "created" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN created REAL")
            session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if "authenticated_at" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN authenticated_at REAL")
            # Older sessions lack provenance. Keep conservative Google attribution where linked;
            # unlink now preserves the caller and converts its attribution back to local.
            if "via_google" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN via_google INTEGER NOT NULL DEFAULT 0")
                db.execute(
                    "UPDATE sessions SET via_google=1"
                    " WHERE user_id IN (SELECT user_id FROM google_identities)"
                )
            flow_columns = {row[1] for row in db.execute("PRAGMA table_info(google_flows)")}
            if "app_return" not in flow_columns:
                db.execute(
                    "ALTER TABLE google_flows ADD COLUMN app_return INTEGER NOT NULL DEFAULT 0"
                )
            if "handoff_hash" not in flow_columns:
                db.execute("ALTER TABLE google_flows ADD COLUMN handoff_hash TEXT")
            db.execute("UPDATE users SET status='approved' WHERE role='admin'")
            db.execute("DROP TABLE IF EXISTS invites")

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

    @staticmethod
    def credentials(username, password):
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username):
            raise AuthError("앱 아이디는 영문, 숫자, 밑줄로 3~32자까지 입력해 주세요.", 400)
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise AuthError("앱 비밀번호는 8~128자로 입력해 주세요.", 400)
        return username.lower()

    def register(self, username, password, application_note=None):
        username = self.credentials(username, password)
        if application_note is not None and (
            not isinstance(application_note, str) or len(application_note) > 200
        ):
            raise AuthError("가입 신청 메모는 200자 이내로 입력해 주세요.", 400)
        application_note = application_note or None
        # Hash before the write transaction: password stretching must not hold
        # SQLite's exclusive writer lock while another friend is signing up.
        hashed = generate_password_hash(password, method=PASSWORD_METHOD)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            user = {
                "id": "mobile_" + secrets.token_hex(16),
                "username": username,
                "role": "member",
                "status": "pending",
                "application_note": application_note,
                "created": self.clock(),
            }
            try:
                db.execute(
                    "INSERT INTO users"
                    " (id, username, password_hash, storage_id, role, status, application_note, created)"
                    " VALUES (?, ?, ?, ?, 'member', 'pending', ?, ?)",
                    (
                        user["id"],
                        username,
                        hashed,
                        -(2**51 + secrets.randbelow(2**50)),
                        application_note,
                        user["created"],
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
                if existing["role"] != "admin":
                    raise AuthError("이미 일반 회원이 사용 중인 앱 아이디예요.", 409)
                db.execute(
                    "UPDATE users SET password_hash=?, status='approved' WHERE id=?",
                    (hashed, existing["id"]),
                )
                db.execute("DELETE FROM sessions WHERE user_id=?", (existing["id"],))
                return {"id": existing["id"], "username": username, "role": "admin"}
            user = {
                "id": "mobile_" + secrets.token_hex(16),
                "username": username,
                "role": "admin",
                "status": "approved",
                "application_note": None,
                "created": self.clock(),
            }
            db.execute(
                "INSERT INTO users"
                " (id, username, password_hash, storage_id, role, status, application_note, created)"
                " VALUES (?, ?, ?, ?, 'admin', 'approved', NULL, ?)",
                (
                    user["id"],
                    username,
                    hashed,
                    -(2**51 + secrets.randbelow(2**50)),
                    user["created"],
                ),
            )
            return user

    def _public_user(self, user):
        values = dict(user)
        role = values.get("role", "member")
        if role not in {"admin", "member"}:
            role = "member"
        status = values.get("status", "rejected")
        if status not in {"pending", "approved", "rejected"}:
            status = "rejected"
        elif role == "admin":
            status = "approved"
        created = values.get("created")
        return {
            "id": user["id"],
            "username": user["username"],
            "role": role,
            "status": status,
            "applicationNote": values.get("application_note"),
            "createdAt": timestamp(created) if created is not None else None,
        }

    def _session(self, db, user, *, via_google=False):
        token = secrets.token_urlsafe(32)
        expires = self.clock() + self.session_ttl
        db.execute("DELETE FROM sessions WHERE expires<=?", (self.clock(),))
        db.execute(
            "INSERT INTO sessions (hash, user_id, expires, authenticated_at, via_google)"
            " VALUES (?, ?, ?, ?, ?)",
            (digest(token), user["id"], expires, self.clock(), int(via_google)),
        )
        return {
            "token": token,
            "user": self._public_user(user),
            "expiresAt": timestamp(expires),
        }

    def verify(self, username, password):
        """The account behind an app ID and password, without starting a session; AuthError if wrong."""
        username = self.credentials(username, password)
        with self.connect() as db:
            user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        # Same expensive primitive for unknown accounts; never disclose
        # whether a particular username has registered.
        valid = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
        if user is None or not valid:
            raise AuthError()
        return user

    def login(self, username, password, expected_role=None):
        user = self.verify(username, password)
        role = self._public_user(user)["role"]
        if expected_role in {"admin", "member"} and role != expected_role:
            raise AuthError(
                "관리자 계정이 아니에요."
                if expected_role == "admin"
                else "회원 로그인 화면에서는 관리자 계정으로 로그인할 수 없어요.",
                403,
            )
        with self.connect() as db:
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
            **public,
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

    def list_users(self):
        with self.connect() as db:
            users = db.execute(
                "SELECT * FROM users ORDER BY"
                " CASE status WHEN 'pending' THEN 0 WHEN 'rejected' THEN 1 ELSE 2 END,"
                " created IS NULL, created, username"
            ).fetchall()
        return [self._public_user(user) for user in users]

    def set_approval(self, user_id, status):
        if not isinstance(status, str) or status not in {"approved", "rejected"}:
            raise AuthError("승인 상태를 확인해 주세요.", 400)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if user is None:
                raise AuthError("회원을 찾지 못했어요.", 404)
            public = self._public_user(user)
            if public["role"] == "admin":
                raise AuthError("관리자 계정의 승인 상태는 바꿀 수 없어요.", 403)
            if public["status"] == "approved" and status == "rejected":
                raise AuthError("이용 중인 회원은 거절 상태로 바꿀 수 없어요.", 409)
            if public["status"] != status:
                db.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
                user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return self._public_user(user)

    def admin_count(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM users WHERE role='admin'").fetchone()[0]

    def delete_user(self, user_id, username, *, rate_limit_keys=()):
        """
        Remove the account, every session it holds and its own rate-limit buckets.

        The last admin is refused: the app would be left with nobody able to
        approve new members. The operator can
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

    def revoke_sessions(self, user_id):
        """End every session of one account: its next request, anywhere, is a 401."""
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def revoke(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE hash=?", (digest(token),))

    def google_start(
        self,
        mode,
        *,
        user_id=None,
        session_token=None,
        initiator_label="알 수 없는 앱",
        initiator_ip="알 수 없음",
        ttl=300,
        app_return=False,
    ):
        """Create a provider flow whose selector and proof are both held only by the app.

        With app_return the browser finishes by handing a one-time value back to the app on the
        same phone, and only the app holding both the ticket and that value receives the result.
        Without it the person types the app's verification code into the browser instead.
        """
        if mode not in {"login", "link"} or (mode == "link") != bool(user_id and session_token):
            raise ValueError("Invalid Google flow")
        selector = secrets.token_urlsafe(32)
        proof = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        verification = "".join(secrets.choice(alphabet) for _ in range(8))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM google_flows WHERE expires<=?", (self.clock(),))
            if mode == "link":
                session_hash = digest(session_token)
                valid = db.execute(
                    "SELECT 1 FROM sessions WHERE hash=? AND user_id=? AND expires>?",
                    (session_hash, user_id, self.clock()),
                ).fetchone()
                if not valid:
                    raise AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.")
            else:
                session_hash = None
            db.execute(
                "INSERT INTO google_flows"
                " (id, proof_hash, state_hash, nonce, code_verifier, verification_hash,"
                " initiator_label, initiator_ip, mode, user_id, session_hash, expires,"
                " app_return)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    digest(selector),
                    digest(proof),
                    digest(state),
                    nonce,
                    code_verifier,
                    digest(verification),
                    initiator_label,
                    initiator_ip,
                    mode,
                    user_id,
                    session_hash,
                    self.clock() + ttl,
                    int(bool(app_return)),
                ),
            )
        result = {
            "ticket": selector + "." + proof,
            "state": state,
            "nonce": nonce,
            "codeVerifier": code_verifier,
            "expiresIn": ttl,
        }
        if not app_return:
            result["verificationCode"] = verification[:4] + "-" + verification[4:]
        return result

    def google_take_callback(self, state):
        """Consume callback state before contacting Google, making callback replay impossible."""
        if not isinstance(state, str) or not 32 <= len(state) <= 128:
            raise AuthError("Google 로그인 요청을 다시 시작해 주세요.", 400)
        expired_session = False
        result = None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT google_flows.*, users.username FROM google_flows"
                " LEFT JOIN users ON users.id=google_flows.user_id"
                " WHERE state_hash=? AND expires>? AND callback_used=0",
                (digest(state), self.clock()),
            ).fetchone()
            if row is None:
                raise AuthError("Google 로그인 요청이 만료됐거나 이미 사용됐어요.", 400)
            db.execute("UPDATE google_flows SET callback_used=1 WHERE id=?", (row["id"],))
            if row["mode"] == "link":
                valid = db.execute(
                    "SELECT 1 FROM sessions WHERE hash=? AND user_id=? AND expires>?",
                    (row["session_hash"], row["user_id"], self.clock()),
                ).fetchone()
                if not valid:
                    db.execute(
                        "UPDATE google_flows SET status='failed', error='session' WHERE id=?",
                        (row["id"],),
                    )
                    expired_session = True
            result = dict(row)
        if expired_session:
            raise AuthError("로그인이 만료됐어요. 다시 로그인해 주세요.", 403)
        return result

    def _google_callback_account(self, db, flow_id, subject, *, app_return):
        """Return the pending flow and its account after the provider callback, or why not."""
        if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
            raise AuthError("Google 계정 정보를 확인할 수 없어요.", 400)
        row = db.execute(
            "SELECT * FROM google_flows"
            " WHERE id=? AND callback_used=1 AND status='pending' AND app_return=?",
            (flow_id, int(app_return)),
        ).fetchone()
        if row is None:
            return False
        if (
            row["mode"] == "link"
            and not db.execute(
                "SELECT 1 FROM sessions WHERE hash=? AND user_id=? AND expires>?",
                (row["session_hash"], row["user_id"], self.clock()),
            ).fetchone()
        ):
            db.execute(
                "UPDATE google_flows SET status='failed', error='session' WHERE id=?",
                (flow_id,),
            )
            return False
        if row["mode"] == "login":
            account = db.execute(
                "SELECT users.id, users.username FROM google_identities"
                " JOIN users ON users.id=google_identities.user_id"
                " WHERE google_identities.subject=?",
                (subject,),
            ).fetchone()
            if account is None:
                db.execute(
                    "UPDATE google_flows SET status='failed', error='unlinked' WHERE id=?",
                    (flow_id,),
                )
                return {"error": "unlinked"}
            return row, account["id"], account["username"]
        account = db.execute("SELECT username FROM users WHERE id=?", (row["user_id"],)).fetchone()
        if account is None:
            return False
        return row, row["user_id"], account["username"]

    def google_prepare_confirmation(self, flow_id, subject, *, secure_cookie=True):
        confirmation = secrets.token_urlsafe(32)
        cookie = secrets.token_urlsafe(32)
        cookie_name = ("__Host-" if secure_cookie else "") + "jari_google_" + secrets.token_hex(6)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = self._google_callback_account(db, flow_id, subject, app_return=False)
            if not isinstance(found, tuple):
                return found
            row, account_id, account_username = found
            changed = db.execute(
                "UPDATE google_flows SET status='confirmation', subject=?,"
                " user_id=?, confirmation_hash=?, confirmation_cookie_hash=?,"
                " confirmation_cookie_name=?"
                " WHERE id=? AND callback_used=1 AND status='pending'",
                (
                    subject,
                    account_id,
                    digest(confirmation),
                    digest(cookie),
                    cookie_name,
                    flow_id,
                ),
            ).rowcount
            return changed == 1 and {
                "confirmation": confirmation,
                "cookie": cookie,
                "cookieName": cookie_name,
                "username": account_username,
                "initiatorLabel": row["initiator_label"],
                "initiatorIp": row["initiator_ip"],
            }

    def google_prepare_handoff(self, flow_id, subject):
        """Finish an app-return flow; the result waits for the app that also holds the handoff."""
        handoff = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = self._google_callback_account(db, flow_id, subject, app_return=True)
            if not isinstance(found, tuple):
                return found
            _, account_id, account_username = found
            changed = db.execute(
                "UPDATE google_flows SET status='complete', subject=?, user_id=?, handoff_hash=?"
                " WHERE id=? AND callback_used=1 AND status='pending'",
                (subject, account_id, digest(handoff), flow_id),
            ).rowcount
            return changed == 1 and {"handoff": handoff, "username": account_username}

    def google_fail_callback(self, flow_id):
        with self.connect() as db:
            db.execute(
                "UPDATE google_flows SET status='failed', error='provider' WHERE id=?",
                (flow_id,),
            )

    @staticmethod
    def _google_ticket(ticket):
        if not isinstance(ticket, str) or ticket.count(".") != 1:
            raise AuthError("Google 로그인 요청을 다시 시작해 주세요.", 400)
        selector, proof = ticket.split(".")
        if not 32 <= len(selector) <= 128 or not 32 <= len(proof) <= 128:
            raise AuthError("Google 로그인 요청을 다시 시작해 주세요.", 400)
        return selector, proof

    def google_poll(self, ticket, handoff=None):
        """Atomically consume a completed flow; login sessions are born only here."""
        selector, proof = self._google_ticket(ticket)
        if handoff is not None and (not isinstance(handoff, str) or not 32 <= len(handoff) <= 128):
            raise AuthError("Google 로그인 요청을 다시 시작해 주세요.", 400)
        result = None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM google_flows WHERE id=?", (digest(selector),)
            ).fetchone()
            if row is None or not secrets.compare_digest(row["proof_hash"], digest(proof)):
                raise AuthError("Google 로그인 요청을 다시 시작해 주세요.", 400)
            handoff_status = row["status"] in {"pending", "confirmation", "complete"}
            if row["expires"] <= self.clock():
                db.execute("DELETE FROM google_flows WHERE id=?", (row["id"],))
                result = {"error": "Google 로그인 요청이 만료됐어요.", "statusCode": 400}
            elif (
                handoff is not None
                and handoff_status
                and not (
                    row["handoff_hash"]
                    and secrets.compare_digest(row["handoff_hash"], digest(handoff))
                )
            ):
                # A handoff meant for another flow reached this app: someone else's sign-in link.
                db.execute("DELETE FROM google_flows WHERE id=?", (row["id"],))
                result = {
                    "error": "이 앱에서 시작한 구글 로그인이 아니에요. 다시 시작해 주세요.",
                    "statusCode": 403,
                }
            elif row["status"] in {"pending", "confirmation"} or (
                row["status"] == "complete" and row["app_return"] and handoff is None
            ):
                return {"status": "pending"}
            elif row["status"] == "failed":
                db.execute("DELETE FROM google_flows WHERE id=?", (row["id"],))
                result = {"error": "Google 로그인을 완료할 수 없어요.", "statusCode": 403}
            elif row["status"] == "delivered":
                result = {"error": "Google 로그인 결과가 이미 전달됐어요.", "statusCode": 400}
            elif row["status"] == "cancelled":
                result = {"error": "Google 로그인 요청이 취소됐어요.", "statusCode": 403}
            elif row["mode"] == "link":
                valid = db.execute(
                    "SELECT 1 FROM sessions WHERE hash=? AND user_id=? AND expires>?",
                    (row["session_hash"], row["user_id"], self.clock()),
                ).fetchone()
                if not valid:
                    db.execute("DELETE FROM google_flows WHERE id=?", (row["id"],))
                    result = {
                        "error": "로그인이 만료됐어요. 다시 로그인해 주세요.",
                        "statusCode": 403,
                    }
                else:
                    try:
                        db.execute(
                            "INSERT INTO google_identities VALUES (?, ?, ?)",
                            (row["subject"], row["user_id"], self.clock()),
                        )
                    except sqlite3.IntegrityError:
                        result = {
                            "error": "이 Google 계정 또는 앱 계정은 이미 연결되어 있어요.",
                            "statusCode": 409,
                        }
                    if result is None:
                        db.execute(
                            "UPDATE google_flows SET status='delivered', delivered_link=1"
                            " WHERE id=?",
                            (row["id"],),
                        )
                        result = {"status": "complete", "linked": True}
                    else:
                        db.execute("DELETE FROM google_flows WHERE id=?", (row["id"],))
            else:
                user = db.execute(
                    "SELECT users.* FROM google_identities"
                    " JOIN users ON users.id=google_identities.user_id"
                    " WHERE google_identities.subject=?",
                    (row["subject"],),
                ).fetchone()
                if user is None:
                    db.execute("DELETE FROM google_flows WHERE id=?", (row["id"],))
                    result = {
                        "error": "먼저 앱 설정에서 기존 계정에 Google 계정을 연결해 주세요.",
                        "statusCode": 403,
                    }
                else:
                    auth = self._session(db, user, via_google=True)
                    db.execute(
                        "UPDATE google_flows SET status='delivered', delivered_session_hash=?"
                        " WHERE id=?",
                        (digest(auth["token"]), row["id"]),
                    )
                    result = {"status": "complete", "auth": auth}
        return result

    @staticmethod
    def _google_verification_code(value):
        if not isinstance(value, str):
            return None
        normalized = value.replace("-", "").replace(" ", "").upper()
        return normalized if re.fullmatch(r"[A-Z2-9]{8}", normalized) else None

    def google_confirm(self, confirmation, cookies, verification_code):
        if not isinstance(confirmation, str) or not 32 <= len(confirmation) <= 128:
            return {"error": "Google 확인 요청을 다시 시작해 주세요.", "statusCode": 400}
        normalized = self._google_verification_code(verification_code)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM google_flows WHERE confirmation_hash=? AND status='confirmation'",
                (digest(confirmation),),
            ).fetchone()
            if row is None:
                return {"error": "Google 확인 요청을 다시 시작해 주세요.", "statusCode": 400}
            cookie = cookies.get(row["confirmation_cookie_name"])
            if not isinstance(cookie, str) or not secrets.compare_digest(
                row["confirmation_cookie_hash"], digest(cookie)
            ):
                return {"error": "이 브라우저의 Google 확인 요청이 아니에요.", "statusCode": 400}
            if row["expires"] <= self.clock():
                db.execute("DELETE FROM google_flows WHERE id=?", (row["id"],))
                return {"error": "Google 로그인 요청이 만료됐어요.", "statusCode": 400}
            if (
                row["mode"] == "link"
                and not db.execute(
                    "SELECT 1 FROM sessions WHERE hash=? AND user_id=? AND expires>?",
                    (row["session_hash"], row["user_id"], self.clock()),
                ).fetchone()
            ):
                db.execute(
                    "UPDATE google_flows SET status='failed', error='session' WHERE id=?",
                    (row["id"],),
                )
                return {"error": "앱 로그인이 만료됐어요.", "statusCode": 403}
            if row["confirmation_attempts"] >= 5:
                return {"error": "확인 코드를 너무 많이 틀렸어요.", "statusCode": 429}
            attempts = row["confirmation_attempts"] + 1
            valid = normalized is not None and secrets.compare_digest(
                row["verification_hash"], digest(normalized)
            )
            if not valid:
                db.execute(
                    "UPDATE google_flows SET confirmation_attempts=?, status=? WHERE id=?",
                    (attempts, "failed" if attempts >= 5 else "confirmation", row["id"]),
                )
                return {
                    "error": "앱에 표시된 확인 코드가 맞지 않아요.",
                    "statusCode": 429 if attempts >= 5 else 403,
                }
            changed = db.execute(
                "UPDATE google_flows SET status='complete', confirmation_attempts=?,"
                " confirmation_hash=NULL, confirmation_cookie_hash=NULL"
                " WHERE id=? AND status='confirmation'",
                (attempts, row["id"]),
            ).rowcount
            return {"confirmed": changed == 1, "cookieName": row["confirmation_cookie_name"]}

    def google_deny(self, confirmation, cookies):
        if not isinstance(confirmation, str) or not 32 <= len(confirmation) <= 128:
            return False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM google_flows WHERE confirmation_hash=? AND status='confirmation'",
                (digest(confirmation),),
            ).fetchone()
            if row is None:
                return False
            cookie = cookies.get(row["confirmation_cookie_name"])
            if not isinstance(cookie, str) or not secrets.compare_digest(
                row["confirmation_cookie_hash"], digest(cookie)
            ):
                return False
            db.execute("DELETE FROM google_flows WHERE id=?", (row["id"],))
            return row["confirmation_cookie_name"]

    def google_cancel(self, ticket):
        selector, proof = self._google_ticket(ticket)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM google_flows WHERE id=?", (digest(selector),)
            ).fetchone()
            if row is None or not secrets.compare_digest(row["proof_hash"], digest(proof)):
                raise AuthError("Google 로그인 요청을 다시 시작해 주세요.", 400)
            if row["expires"] <= self.clock():
                db.execute("DELETE FROM google_flows WHERE id=?", (row["id"],))
                return True
            if row["status"] == "cancelled":
                return True
            if row["delivered_session_hash"]:
                db.execute("DELETE FROM sessions WHERE hash=?", (row["delivered_session_hash"],))
            if row["delivered_link"]:
                db.execute(
                    "DELETE FROM google_identities WHERE user_id=? AND subject=?",
                    (row["user_id"], row["subject"]),
                )
            db.execute(
                "UPDATE google_flows SET status='cancelled', subject=NULL, error=NULL,"
                " confirmation_hash=NULL, confirmation_cookie_hash=NULL, handoff_hash=NULL,"
                " delivered_session_hash=NULL, delivered_link=0, callback_used=1 WHERE id=?",
                (row["id"],),
            )
            return True

    def google_acknowledge(self, ticket):
        """Forget a delivered rollback receipt after the app persists its result."""
        selector, proof = self._google_ticket(ticket)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT proof_hash, status FROM google_flows WHERE id=?", (digest(selector),)
            ).fetchone()
            if (
                row is None
                or not secrets.compare_digest(row["proof_hash"], digest(proof))
                or row["status"] != "delivered"
            ):
                raise AuthError("Google 로그인 요청을 다시 시작해 주세요.", 400)
            db.execute("DELETE FROM google_flows WHERE id=?", (digest(selector),))
            return True

    def google_link(self, user_id, subject):
        """Link one subject to one existing account; used by the atomic flow and migrations/tests."""
        with self.connect() as db:
            db.execute(
                "INSERT INTO google_identities VALUES (?, ?, ?)",
                (subject, user_id, self.clock()),
            )

    def google_linked(self, subject):
        with self.connect() as db:
            row = db.execute(
                "SELECT user_id FROM google_identities WHERE subject=?", (subject,)
            ).fetchone()
        return row and row["user_id"]

    def google_status(self, user_id):
        with self.connect() as db:
            return (
                db.execute("SELECT 1 FROM google_identities WHERE user_id=?", (user_id,)).fetchone()
                is not None
            )

    def session_via_google(self, token):
        with self.connect() as db:
            row = db.execute(
                "SELECT via_google FROM sessions WHERE hash=?", (digest(token),)
            ).fetchone()
        return bool(row and row["via_google"])

    def google_unlink(self, user_id, token):
        """
        Remove the Google sign-in and keep only the phone that requested it.

        The caller becomes a local session because every account has an app password. Other sessions end,
        including ones previously handed out by Google. With nothing linked, no session is touched.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM google_flows WHERE user_id=?", (user_id,))
            unlinked = (
                db.execute("DELETE FROM google_identities WHERE user_id=?", (user_id,)).rowcount > 0
            )
            if not unlinked:
                return {"linked": False, "signedOut": False}
            caller_hash = digest(token)
            db.execute(
                "UPDATE sessions SET via_google=0 WHERE user_id=? AND hash=?",
                (user_id, caller_hash),
            )
            db.execute("DELETE FROM sessions WHERE user_id=? AND hash<>?", (user_id, caller_hash))
            return {"linked": False, "signedOut": False}

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
