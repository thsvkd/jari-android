"""Durable private inbox and optional, retryable FCM outbox."""

import re
import secrets
import sqlite3

from korail_bot.mobile.identity import AuthError, digest, timestamp
from korail_bot.utils.crypto import SecretBox


class Notifications:
    def __init__(self, identity, *, secret, push=None, storage=None, push_enabled=False):
        self.identity = identity
        self.box = SecretBox(secret)
        self.push = push
        self.push_enabled = push_enabled
        self.storage = storage
        with identity.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, text TEXT NOT NULL,
                    created REAL NOT NULL, kind TEXT NOT NULL, dedupe TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS events_owner ON events(owner, created);
                CREATE TABLE IF NOT EXISTS devices (
                    hash TEXT PRIMARY KEY, owner INTEGER NOT NULL, token TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT REFERENCES events(id), device_hash TEXT REFERENCES devices(hash) ON DELETE CASCADE,
                    next_try REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(event_id, device_hash)
                );
            """)

    @property
    def push_available(self):
        return self.push is not None or self.push_enabled

    def _safe_text(self, owner, text):
        if self.storage:
            try:
                account = self.storage.get_onboarded_account(owner)
                credentials = self.storage.get_resume_credentials(owner)
                for value in ([account.korail_pw, account.korail_id] if account else []) + list(
                    credentials or []
                ):
                    if value:
                        text = text.replace(value, "[보호된 정보]")
            except Exception:
                return "예약 상태가 변경되었습니다. 앱에서 확인해주세요."
        text = re.sub(r"01[016789][- ]?\d{3,4}[- ]?\d{4}", "[전화번호]", str(text))
        # Shared services still speak the old chat vocabulary. Make their
        # suggested actions usable from this standalone application.
        for command, label in {
            "/cancel": "검색 중지",
            "/start": "새 검색",
            "/register": "철도 계정 연결",
            "/notify_off": "알림 설정",
        }.items():
            text = text.replace(command, label)
        return text[:8000]

    def send_message(self, chat_id, text, **kwargs):
        return bool(self.publish(chat_id, text))

    def send_and_get_id(self, chat_id, text, **kwargs):
        return self.publish(chat_id, text)

    def publish(self, owner, text, *, kind="booking", dedupe=None):
        event_id = secrets.token_hex(16)
        now = self.identity.clock()
        text = self._safe_text(owner, text)
        with self.identity.connect() as db:
            try:
                db.execute(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                    (event_id, owner, text, now, kind, dedupe),
                )
            except sqlite3.IntegrityError:
                if dedupe:
                    return None
                raise
            if self.push_available:
                db.execute(
                    "INSERT INTO outbox(event_id, device_hash, next_try) SELECT ?, hash, ? FROM devices WHERE owner=?",
                    (event_id, now, owner),
                )
        return event_id

    def items(self, owner):
        with self.identity.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE owner=? ORDER BY created DESC, rowid DESC LIMIT 100",
                (owner,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "text": row["text"],
                "createdAt": timestamp(row["created"]),
                "kind": row["kind"],
            }
            for row in rows
        ]

    def register_device(self, owner, token):
        with self.identity.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT owner FROM devices WHERE hash=?", (digest(token),)
            ).fetchone()
            if existing and existing["owner"] != owner:
                raise AuthError(
                    "다른 앱 계정에 연결된 기기입니다. 이전 계정에서 해제해주세요.", 409
                )
            count = db.execute("SELECT count(*) FROM devices WHERE owner=?", (owner,)).fetchone()[0]
            if not existing and count >= 10:
                raise AuthError("등록 가능한 기기 수를 초과했습니다.", 409)
            db.execute(
                "INSERT OR IGNORE INTO devices VALUES (?, ?, ?)",
                (digest(token), owner, self.box.encrypt(token)),
            )

    def remove_device(self, owner, token):
        with self.identity.connect() as db:
            db.execute("DELETE FROM devices WHERE hash=? AND owner=?", (digest(token), owner))

    def deliver(self):
        if self.push is None:
            return 0
        now = self.identity.clock()
        with self.identity.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT o.*, d.token FROM outbox o JOIN devices d ON d.hash=o.device_hash WHERE o.next_try<=? AND o.attempts<10 LIMIT 5",
                (now,),
            ).fetchall()
            # Claim before the network call. Another process or restart retries
            # after the lease rather than sending all pending items twice.
            for row in rows:
                db.execute(
                    "UPDATE outbox SET next_try=?, attempts=attempts+1 WHERE event_id=? AND device_hash=?",
                    (now + 120, row["event_id"], row["device_hash"]),
                )
        sent = 0
        for row in rows:
            token = self.box.decrypt(row["token"])
            try:
                if token is None:
                    raise ValueError("Unreadable device token")
                self.push(token, row["event_id"])
            except Exception:
                # No exception text: a provider can include the private token.
                with self.identity.connect() as db:
                    db.execute(
                        "UPDATE outbox SET next_try=? WHERE event_id=? AND device_hash=?",
                        (
                            now + min(3600, 30 * 2 ** row["attempts"]),
                            row["event_id"],
                            row["device_hash"],
                        ),
                    )
                continue
            with self.identity.connect() as db:
                db.execute(
                    "DELETE FROM outbox WHERE event_id=? AND device_hash=?",
                    (row["event_id"], row["device_hash"]),
                )
            sent += 1
        return sent


def configured_fcm(credentials_path):
    """Load explicitly supplied credentials; never creates a cloud resource."""
    if not credentials_path:
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging
    except ImportError as exc:
        raise ValueError("Install the mobile-push extra to enable configured FCM") from exc
    app = firebase_admin.initialize_app(
        credentials.Certificate(credentials_path), options={"httpTimeout": 10}, name="teum"
    )

    def send(token, event_id):
        messaging.send(
            messaging.Message(
                token=token,
                notification=messaging.Notification(
                    title="틈", body="새 알림이 도착했습니다. 앱에서 확인해주세요."
                ),
                data={"eventId": event_id},
                android=messaging.AndroidConfig(priority="high"),
            ),
            app=app,
        )

    return send
