"""
Agent connections: an OAuth 2.1 authorization server whose consent screen is the app.

An MCP client registers itself (DCR), opens /oauth/authorize in a browser and
gets an eight-letter code there. The user types that code into the app, which
approves the request for the signed-in account; the browser that started it
then picks up the authorization code and returns to the client. No password is
ever typed into the browser, and nobody copies a token by hand.

Every secret (user code, browser cookie, authorization code, tokens) is kept as
its digest only. The routes here answer in RFC 6749 shapes ({"error": code}),
never through the app's Korean error handlers: MCP clients read those codes.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from html import escape
from urllib.parse import urlencode, urlsplit

from flask import Response, jsonify, redirect, request

from korail_bot.mobile.identity import digest, timestamp
from korail_bot.mobile.pages import _STYLE, APP_NAME

READ, BOOK = "jari.read", "jari.book"
PRM_PATH = "/.well-known/oauth-protected-resource/api/mobile/mcp"
SCOPES = [READ, BOOK]
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
REQUEST_TTL = 600
CODE_TTL = 60
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
GRANT_IDLE_TTL = 90 * 86400
UNCONNECTED_CLIENT_TTL = 86400
CLAUDE_CALLBACKS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
SCOPE = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]+( [\x21\x23-\x5b\x5d-\x7e]+)*")
COUNTRY = re.compile(r"[A-Z]{2}")
# Live requests at most: past this, authorize shows an error page instead of making more.
MAX_PENDING = 2000
LOCAL_PROGRAM = "이 컴퓨터의 프로그램"


class OAuthError(Exception):
    def __init__(self, error, status=400):
        super().__init__(error)
        self.error = error
        self.status = status


def redirect_target(uri):
    """
    What a redirect URI is matched by, or None if this server will not follow it.

    Claude's callbacks match exactly. Other clients run on the user's own
    computer and listen on a loopback port they pick per run (RFC 8252 §7.3),
    so the port is left out and everything else must match.
    ponytail: another hosted client's https callback is added to CLAUDE_CALLBACKS.
    """
    if not isinstance(uri, str):
        return None
    if uri in CLAUDE_CALLBACKS:
        return uri
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 - raises on a port that is not a number
    except ValueError:
        return None
    if (
        parts.scheme != "http"
        or parts.hostname not in LOOPBACK_HOSTS
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
    ):
        return None
    return f"http://{parts.hostname}{parts.path}"


def describe_client(name, redirect_uri):
    """Who asked, as the app shows it: the name the client chose and where it returns."""
    return {
        "name": name,
        "host": urlsplit(redirect_uri).hostname,
        "local": redirect_uri not in CLAUDE_CALLBACKS,
    }


def s256(verifier):
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


def client_address():
    # As for app logins (api.py auth): Cloudflare overwrites this header.
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "unknown"


def client_country():
    """Where Cloudflare places this request (CF-IPCountry), or None: two capital letters only."""
    country = request.headers.get("CF-IPCountry", "")
    return country if COUNTRY.fullmatch(country) else None


class AgentStore:
    """Clients, pending requests, connections (grants) and tokens, in the IdentityStore database."""

    def __init__(self, identity, issuer):
        self.identity = identity
        self.issuer = issuer
        self.resource = issuer + "/api/mobile/mcp"

    def _sweep(self, db, now):
        db.execute("DELETE FROM oauth_requests WHERE expires<=?", (now - REQUEST_TTL,))
        db.execute("DELETE FROM oauth_tokens WHERE expires<=?", (now,))
        # Approved in the app but never exchanged: the browser was closed. The
        # latest such a grant can still be activated is its request's expiry
        # plus the code's lifetime.
        db.execute(
            "DELETE FROM oauth_grants WHERE expires<=? OR (active=0 AND created<=?)",
            (now, now - REQUEST_TTL - CODE_TTL),
        )
        db.execute(
            "DELETE FROM oauth_clients WHERE created<=?"
            " AND id NOT IN (SELECT client_id FROM oauth_grants)",
            (now - UNCONNECTED_CLIENT_TTL,),
        )

    def register(self, name, redirect_uris):
        now = self.identity.clock()
        client_id = secrets.token_urlsafe(32)
        with self.identity.connect() as db:
            self._sweep(db, now)
            db.execute(
                "INSERT INTO oauth_clients VALUES (?, ?, ?, ?)",
                (client_id, name, json.dumps(redirect_uris), now),
            )
        return client_id, int(now)

    def client(self, client_id):
        """A registered client; one that never connected is forgotten after a day."""
        with self.identity.connect() as db:
            row = db.execute(
                "SELECT * FROM oauth_clients WHERE id=? AND (created>?"
                " OR id IN (SELECT client_id FROM oauth_grants))",
                (client_id, self.identity.clock() - UNCONNECTED_CLIENT_TTL),
            ).fetchone()
        return row and {**dict(row), "redirect_uris": json.loads(row["redirect_uris"])}

    def start(self, client_id, redirect_uri, challenge, state, country):
        """
        A pending request: its id, the code the user types and the browser's secret.
        None when MAX_PENDING requests are already waiting.
        """
        now = self.identity.clock()
        request_id = secrets.token_urlsafe(16)
        browser = secrets.token_urlsafe(32)
        # 31^8 codes; UNIQUE only collides with another live or recent request.
        for _ in range(5):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            try:
                with self.identity.connect() as db:
                    self._sweep(db, now)
                    waiting = db.execute(
                        "SELECT count(*) FROM oauth_requests WHERE status='pending' AND expires>?",
                        (now,),
                    ).fetchone()[0]
                    if waiting >= MAX_PENDING:
                        return None
                    db.execute(
                        "INSERT INTO oauth_requests (id, user_code_hash, browser_hash, client_id,"
                        " redirect_uri, code_challenge, state, country, created, expires)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            request_id,
                            digest(code),
                            digest(browser),
                            client_id,
                            redirect_uri,
                            challenge,
                            state,
                            country,
                            now,
                            now + REQUEST_TTL,
                        ),
                    )
                return request_id, f"{code[:4]}-{code[4:]}", browser
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("Could not allocate a unique user code")

    def poll(self, request_id, browser):
        """What the browser that started the request may know; None for any other browser."""
        now = self.identity.clock()
        with self.identity.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM oauth_requests WHERE id=?", (request_id,)).fetchone()
            if row is None or not hmac.compare_digest(row["browser_hash"], digest(browser)):
                return None
            if row["delivered"] or row["expires"] <= now:
                return {"status": "expired"}
            if row["status"] == "pending":
                return {"status": "pending"}
            # Made only now, so its sixty seconds start when the browser has it and
            # nothing ever stores it in the clear.
            code = secrets.token_urlsafe(32) if row["status"] == "approved" else None
            params = {"code": code} if code else {"error": "access_denied"}
            db.execute(
                "UPDATE oauth_requests SET delivered=1, code_hash=?, code_expires=? WHERE id=?",
                (code and digest(code), now + CODE_TTL, request_id),
            )
        if row["state"] is not None:
            params["state"] = row["state"]
        params["iss"] = self.issuer
        return {"status": row["status"], "redirect": row["redirect_uri"] + "?" + urlencode(params)}

    def lookup(self, user_id, code, country):
        """
        The pending request behind a code the user typed, now held for that user:
        only they can approve or deny it from here on. `country` is where the
        phone is; a browser elsewhere is the device-code phishing sign.
        """
        canonical = re.sub(r"[\s-]", "", code).upper() if isinstance(code, str) else ""
        now = self.identity.clock()
        with self.identity.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT oauth_requests.*, oauth_clients.name FROM oauth_requests"
                " JOIN oauth_clients ON oauth_clients.id=oauth_requests.client_id"
                " WHERE user_code_hash=? AND status='pending' AND expires>?"
                " AND (user_id IS NULL OR user_id=?)",
                (digest(canonical), now, user_id),
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE oauth_requests SET user_id=? WHERE id=?", (user_id, row["id"]))
        return {
            "requestId": row["id"],
            "client": describe_client(row["name"], row["redirect_uri"]),
            "requestedAt": timestamp(row["created"]),
            "expiresAt": timestamp(row["expires"]),
            "requestCountry": row["country"],
            "countryMismatch": bool(row["country"] and country and row["country"] != country),
        }

    def decide(self, user_id, request_id, scope):
        """Approve with `scope`, or deny with None. False if the user holds no such request."""
        now = self.identity.clock()
        with self.identity.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM oauth_requests WHERE id=? AND user_id=? AND status='pending'"
                " AND expires>?",
                (request_id, user_id, now),
            ).fetchone()
            if row is None:
                return False
            if scope is None:
                db.execute("UPDATE oauth_requests SET status='denied' WHERE id=?", (request_id,))
                return True
            # Inactive until the client exchanges the code: a browser closed now
            # leaves no connection in the app's list.
            grant_id = secrets.token_urlsafe(16)
            db.execute(
                "INSERT INTO oauth_grants (id, user_id, client_id, redirect_uri, scope, created,"
                " expires) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    grant_id,
                    user_id,
                    row["client_id"],
                    row["redirect_uri"],
                    scope,
                    now,
                    now + GRANT_IDLE_TTL,
                ),
            )
            db.execute(
                "UPDATE oauth_requests SET status='approved', grant_id=? WHERE id=?",
                (grant_id, request_id),
            )
        return True

    def _issue(self, db, grant_id, scope, now):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.executemany(
            "INSERT INTO oauth_tokens (hash, grant_id, kind, resource, expires) VALUES (?, ?, ?, ?, ?)",
            [
                (digest(access), grant_id, "access", self.resource, now + ACCESS_TTL),
                (digest(refresh), grant_id, "refresh", self.resource, now + REFRESH_TTL),
            ],
        )
        db.execute(
            "UPDATE oauth_grants SET active=1, expires=? WHERE id=?",
            (now + GRANT_IDLE_TTL, grant_id),
        )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": refresh,
            "scope": scope,
        }

    def exchange(self, form):
        if not all(form.get(key) for key in ("code", "redirect_uri", "client_id", "code_verifier")):
            raise OAuthError("invalid_request")
        verifier = form["code_verifier"]
        now = self.identity.clock()
        issued = None
        with self.identity.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT oauth_requests.*, oauth_grants.scope, oauth_grants.user_id AS owner"
                " FROM oauth_requests"
                " LEFT JOIN oauth_grants ON oauth_grants.id=oauth_requests.grant_id"
                " WHERE code_hash=?",
                (digest(form["code"]),),
            ).fetchone()
            if row is not None and row["status"] == "exchanged":
                # A code used twice was stolen by one of the two: end what it gave.
                db.execute("DELETE FROM oauth_grants WHERE id=?", (row["grant_id"],))
            elif (
                row is not None
                and row["status"] == "approved"
                and row["scope"] is not None
                and row["code_expires"] > now
                and row["client_id"] == form["client_id"]
                and row["redirect_uri"] == form["redirect_uri"]
                and VERIFIER.fullmatch(verifier)
                and hmac.compare_digest(s256(verifier), row["code_challenge"])
            ):
                db.execute("UPDATE oauth_requests SET status='exchanged' WHERE id=?", (row["id"],))
                # Clients that log in again (Claude Code does on every reconnect) would
                # otherwise stack connections: one per user and client.
                db.execute(
                    "DELETE FROM oauth_grants WHERE user_id=? AND client_id=? AND id<>?",
                    (row["owner"], row["client_id"], row["grant_id"]),
                )
                issued = self._issue(db, row["grant_id"], row["scope"], now)
        if issued is None:
            raise OAuthError("invalid_grant")
        return issued

    def refresh(self, form):
        if not form.get("refresh_token") or not form.get("client_id"):
            raise OAuthError("invalid_request")
        now = self.identity.clock()
        issued = None
        with self.identity.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT oauth_tokens.*, oauth_grants.client_id, oauth_grants.scope,"
                " oauth_grants.active, oauth_grants.expires AS grant_expires"
                " FROM oauth_tokens JOIN oauth_grants ON oauth_grants.id=oauth_tokens.grant_id"
                " WHERE hash=? AND kind='refresh'",
                (digest(form["refresh_token"]),),
            ).fetchone()
            if row is not None and row["rotated"]:
                # Both the thief and the client hold this family now: end it.
                db.execute("DELETE FROM oauth_grants WHERE id=?", (row["grant_id"],))
            elif (
                row is not None
                and row["expires"] > now
                and row["grant_expires"] > now
                and row["active"]
                and row["client_id"] == form["client_id"]
                and row["resource"] == self.resource
            ):
                # Only this generation stays, as the replay tripwire; older ones and
                # every access token of the connection go.
                db.execute(
                    "DELETE FROM oauth_tokens WHERE grant_id=? AND (kind='access' OR rotated=1)",
                    (row["grant_id"],),
                )
                db.execute("UPDATE oauth_tokens SET rotated=1 WHERE hash=?", (row["hash"],))
                issued = self._issue(db, row["grant_id"], row["scope"], now)
        if issued is None:
            raise OAuthError("invalid_grant")
        return issued

    def revoke(self, token, client_id):
        """RFC 7009: a refresh token ends its connection; an access token only itself."""
        with self.identity.connect() as db:
            row = db.execute(
                "SELECT oauth_tokens.kind, oauth_tokens.grant_id, oauth_grants.client_id"
                " FROM oauth_tokens JOIN oauth_grants ON oauth_grants.id=oauth_tokens.grant_id"
                " WHERE hash=?",
                (digest(token),),
            ).fetchone()
            if row is None or (client_id and client_id != row["client_id"]):
                return
            if row["kind"] == "refresh":
                db.execute("DELETE FROM oauth_grants WHERE id=?", (row["grant_id"],))
            else:
                db.execute("DELETE FROM oauth_tokens WHERE hash=?", (digest(token),))

    def agent(self, token):
        """The account and scope behind an MCP access token, or None."""
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            return None
        now = self.identity.clock()
        with self.identity.connect() as db:
            row = db.execute(
                "SELECT users.id, users.storage_id, oauth_grants.scope, oauth_grants.id AS grant_id"
                " FROM oauth_tokens JOIN oauth_grants ON oauth_grants.id=oauth_tokens.grant_id"
                " JOIN users ON users.id=oauth_grants.user_id"
                " WHERE hash=? AND kind='access' AND oauth_tokens.expires>? AND resource=?"
                " AND active=1 AND oauth_grants.expires>?",
                (digest(token), now, self.resource, now),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE oauth_grants SET last_used=?, expires=? WHERE id=?",
                (now, now + GRANT_IDLE_TTL, row["grant_id"]),
            )
        return dict(row)

    def connections(self, user_id):
        with self.identity.connect() as db:
            rows = db.execute(
                "SELECT oauth_grants.*, oauth_clients.name FROM oauth_grants"
                " JOIN oauth_clients ON oauth_clients.id=oauth_grants.client_id"
                " WHERE user_id=? AND active=1 AND expires>? ORDER BY created DESC",
                (user_id, self.identity.clock()),
            ).fetchall()
        return [
            {
                "id": row["id"],
                **describe_client(row["name"], row["redirect_uri"]),
                "allowBooking": row["scope"] == BOOK,
                "createdAt": timestamp(row["created"]),
                "lastUsedAt": row["last_used"] and timestamp(row["last_used"]),
            }
            for row in rows
        ]

    def set_booking(self, user_id, grant_id, allow):
        with self.identity.connect() as db:
            return db.execute(
                "UPDATE oauth_grants SET scope=? WHERE id=? AND user_id=? AND active=1",
                (BOOK if allow else READ, grant_id, user_id),
            ).rowcount

    def disconnect(self, user_id, grant_id):
        with self.identity.connect() as db:
            return db.execute(
                "DELETE FROM oauth_grants WHERE id=? AND user_id=?", (grant_id, user_id)
            ).rowcount


_PAGE_STYLE = """
.code { margin: 20px 0; font: 700 2.4rem/1.2 ui-monospace, Menlo, Consolas, monospace; letter-spacing: .12em; text-align: center; }
"""


def _page(nonce, body, script=""):
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{APP_NAME} 에이전트 연결</title>"
        f'<style nonce="{nonce}">{_STYLE}{_PAGE_STYLE}</style></head><body><main>'
        f'<header><span class="app">{APP_NAME}</span><span class="dev">에이전트 연결</span></header>'
        f"{body}</main>{script}</body></html>"
    )


# The request id is token_urlsafe, so it needs no quoting inside the script.
_POLL_SCRIPT = """
const id = "%s";
const status = document.getElementById("status");
const deadline = Date.now() + %d * 1000;
async function poll() {
  try {
    const reply = await fetch("/oauth/authorize/poll?request=" + id, { cache: "no-store" });
    // Only 404 means this request is gone; a 429 or 5xx is tried again on the next round.
    const body = reply.ok ? await reply.json() : { status: reply.status === 404 ? "expired" : "pending" };
    if (body.redirect) {
      status.textContent = body.status === "approved" ? "연결했어요. 에이전트로 돌아가요." : "연결을 거절했어요.";
      location.replace(body.redirect);
      return;
    }
    if (body.status !== "pending") {
      status.textContent = "코드가 만료됐어요. 에이전트에서 연결을 다시 시작해 주세요.";
      return;
    }
  } catch (error) {
    // 잠깐 끊긴 연결은 다음 확인에서 다시 시도해요.
  }
  const remaining = deadline - Date.now();
  const left = remaining > 0 ? Math.round(remaining / 1000) : 0;
  status.textContent = "남은 시간 " + Math.floor(left / 60) + "분 " + (left %% 60) + "초";
  setTimeout(poll, 2000);
}
poll();
"""


def install(app, identity, public_url):
    """The OAuth routes at the root of the public URL. Returns the store the app routes use."""
    store = AgentStore(identity, public_url)
    secure = public_url.startswith("https://")
    # __Host- requires Secure, which a browser drops for plain-http test servers.
    cookie = "__Host-jari_oauth" if secure else "jari_oauth"

    @app.errorhandler(OAuthError)
    def oauth_error(exc):
        return jsonify(error=exc.error), exc.status

    def limited(key, limit, window):
        if not identity.allow(key, limit, window):
            raise OAuthError("temporarily_unavailable", 429)

    def html(body, status, nonce):
        response = Response(body, status, mimetype="text/html")
        response.headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}';"
            " connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def protected_resource():
        return jsonify(
            resource=store.resource,
            authorization_servers=[public_url],
            scopes_supported=SCOPES,
            bearer_methods_supported=["header"],
            resource_name=APP_NAME,
        )

    app.add_url_rule(PRM_PATH, "prm", protected_resource)
    app.add_url_rule("/.well-known/oauth-protected-resource", "prm_root", protected_resource)

    @app.get("/.well-known/oauth-authorization-server")
    def authorization_server():
        return jsonify(
            issuer=public_url,
            authorization_endpoint=public_url + "/oauth/authorize",
            token_endpoint=public_url + "/oauth/token",
            registration_endpoint=public_url + "/oauth/register",
            revocation_endpoint=public_url + "/oauth/revoke",
            response_types_supported=["code"],
            grant_types_supported=["authorization_code", "refresh_token"],
            code_challenge_methods_supported=["S256"],
            token_endpoint_auth_methods_supported=["none"],
            scopes_supported=SCOPES,
            authorization_response_iss_parameter_supported=True,
        )

    @app.post("/oauth/register")
    def register_client():
        limited("oauth-register:" + client_address(), 10, 3600)
        limited("oauth-register", 200, 3600)
        metadata = request.get_json(silent=True)
        if not isinstance(metadata, dict):
            raise OAuthError("invalid_client_metadata")
        uris = metadata.get("redirect_uris")
        if (
            not isinstance(uris, list)
            or not 1 <= len(uris) <= 5
            or not all(map(redirect_target, uris))
        ):
            raise OAuthError("invalid_redirect_uri")

        def only(key, allowed):
            value = metadata.get(key, [])
            return isinstance(value, list) and all(
                isinstance(item, str) and item in allowed for item in value
            )

        if (
            metadata.get("token_endpoint_auth_method", "none") != "none"
            or not only("grant_types", {"authorization_code", "refresh_token"})
            or not only("response_types", {"code"})
        ):
            raise OAuthError("invalid_client_metadata")
        name = metadata.get("client_name")
        # Shown in the app as text only, and never trusted: anyone can call themselves Claude.
        name = "".join(c for c in name if c.isprintable())[:64] if isinstance(name, str) else ""
        name = name.strip() or "이름 없는 에이전트"
        client_id, issued_at = store.register(name, uris)
        return jsonify(
            client_id=client_id,
            client_id_issued_at=issued_at,
            client_name=name,
            redirect_uris=uris,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ), 201

    @app.get("/oauth/authorize")
    def authorize():
        args = request.args
        nonce = secrets.token_urlsafe(16)
        client = store.client(args.get("client_id", ""))
        redirect_uri = args.get("redirect_uri", "")
        target = redirect_target(redirect_uri)
        if (
            client is None
            or target is None
            or target not in map(redirect_target, client["redirect_uris"])
        ):
            # Never redirect for a client or a redirect this server cannot vouch for.
            body = "<p>연결 요청을 확인할 수 없어요. 에이전트에서 연결을 다시 시작해 주세요.</p>"
            return html(_page(nonce, body), 400, nonce)
        # A person reads this answer, so it is a page like the others, not OAuth JSON.
        if not (
            identity.allow("oauth-authorize:" + client_address(), 60, 60)
            and identity.allow("oauth-authorize", 300, 60)
        ):
            body = "<p>요청이 너무 많아요. 잠시 후 다시 시도해 주세요.</p>"
            return html(_page(nonce, body), 429, nonce)
        state = args.get("state")
        error = None
        if args.get("response_type") != "code":
            error = "unsupported_response_type"
        elif args.get("code_challenge_method") != "S256" or not CHALLENGE.fullmatch(
            args.get("code_challenge", "")
        ):
            error = "invalid_request"
        elif args.get("resource", store.resource) != store.resource:
            error = "invalid_target"
        elif "scope" in args and not SCOPE.fullmatch(args["scope"]):
            error = "invalid_scope"
        if error:
            query = {"error": error, **({"state": state} if state is not None else {})}
            return redirect(f"{redirect_uri}?{urlencode({**query, 'iss': public_url})}", 302)

        started = store.start(
            client["id"], redirect_uri, args["code_challenge"], state, client_country()
        )
        if started is None:
            body = "<p>지금은 연결 요청이 너무 많아요. 잠시 후 다시 시도해 주세요.</p>"
            return html(_page(nonce, body), 503, nonce)
        request_id, code, browser = started
        who = describe_client(client["name"], redirect_uri)
        asker = LOCAL_PROGRAM if who["local"] else who["host"]
        # The state is the client's own string: it goes back in the redirect
        # only, never into this page.
        body = (
            f"<p><b>{escape(client['name'])} — {escape(asker)}</b></p>"
            f"<p>이 프로그램이 {APP_NAME}에 연결하려고 해요.</p>"
            f'<p class="code">{escape(code)}</p>'
            f"<p>{APP_NAME} 앱 → 설정 → 에이전트 연결에서 이 코드를 입력해 주세요.</p>"
            '<p class="muted">직접 연 브라우저가 아니라면 이 코드를 아무에게도 알려 주지 마세요.</p>'
            f'<p id="status" class="muted">{REQUEST_TTL // 60}분 안에 입력해 주세요.</p>'
        )
        script = f'<script nonce="{nonce}">{_POLL_SCRIPT % (request_id, REQUEST_TTL)}</script>'
        response = html(_page(nonce, body, script), 200, nonce)
        # ponytail: one cookie per browser, so a second connection started in the same
        # browser orphans the first page (it reads as expired); key the name by request if that bites.
        response.set_cookie(
            cookie,
            browser,
            max_age=REQUEST_TTL,
            path="/",
            secure=secure,
            httponly=True,
            samesite="Lax",
        )
        return response

    @app.get("/oauth/authorize/poll")
    def poll():
        limited("oauth-poll:" + client_address(), 120, 60)
        limited("oauth-poll", 3000, 60)
        result = store.poll(request.args.get("request", ""), request.cookies.get(cookie, ""))
        if result is None:
            raise OAuthError("invalid_request", 404)
        return jsonify(result)

    def token_limits():
        limited("oauth-token:" + client_address(), 60, 60)
        if request.mimetype != "application/x-www-form-urlencoded":
            raise OAuthError("invalid_request")
        # Per client rather than one bucket for everyone, which anyone could drain. An
        # unknown client_id gets no bucket: it would be a free key per request.
        client_id = request.form.get("client_id", "")
        if client_id and store.client(client_id):
            limited("oauth-token-client:" + client_id, 60, 60)

    @app.post("/oauth/token")
    def token():
        token_limits()
        form = request.form
        if form.get("resource", store.resource) != store.resource:
            raise OAuthError("invalid_target")
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            issued = store.exchange(form)
        elif grant_type == "refresh_token":
            issued = store.refresh(form)
        else:
            raise OAuthError("unsupported_grant_type" if grant_type else "invalid_request")
        response = jsonify(issued)
        response.headers["Pragma"] = "no-cache"
        return response

    @app.post("/oauth/revoke")
    def revoke():
        token_limits()
        if request.form.get("token"):
            store.revoke(request.form["token"], request.form.get("client_id"))
        return jsonify({})

    return store
