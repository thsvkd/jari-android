"""
Agent connections: an OAuth 2.1 authorization server that signs in with the app account.

An MCP client registers itself (DCR) and opens /oauth/authorize in a browser.
That page names the client and asks for the app ID and password; "로그인하고
허용" checks them without starting an app session and returns to the client with
a sixty-second, one-time authorization code for a read-only connection. Booking
is turned on later, in the app's list of connections. Nobody copies a token by
hand, and the password guesses spend the app login's own budget.

Every secret (authorization code, tokens) is kept as its digest only. The JSON
routes here answer in RFC 6749 shapes ({"error": code}), never through the app's
Korean error handlers: MCP clients read those codes.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
from html import escape
from urllib.parse import urlencode, urlsplit

from flask import Response, jsonify, redirect, request

from korail_bot.mobile.identity import AuthError, digest, timestamp
from korail_bot.mobile.pages import _STYLE, APP_NAME

READ, BOOK = "jari.read", "jari.book"
MCP_PATH = "/mcp"
PRM_PATH = "/.well-known/oauth-protected-resource" + MCP_PATH
SCOPES = [READ, BOOK]
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
# The authorization request travels from the page's GET to its POST in hidden fields.
REQUEST_FIELDS = (
    "response_type",
    "client_id",
    "redirect_uri",
    "code_challenge",
    "code_challenge_method",
    "state",
    "scope",
    "resource",
)
FORM_TTL = 3600
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


class AgentStore:
    """Clients, authorization codes, connections (grants) and tokens, in the IdentityStore database."""

    def __init__(self, identity, issuer):
        self.identity = identity
        self.issuer = issuer
        self.resource = issuer + MCP_PATH

    def _sweep(self, db, now):
        # A used code stays an hour past its expiry, so replaying it still ends what it gave.
        db.execute("DELETE FROM oauth_codes WHERE expires<=?", (now - 3600,))
        db.execute("DELETE FROM oauth_tokens WHERE expires<=?", (now,))
        # Allowed in the browser but never exchanged: its code has expired by now.
        db.execute(
            "DELETE FROM oauth_grants WHERE expires<=? OR (active=0 AND created<=?)",
            (now, now - CODE_TTL),
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

    def allow(self, user_id, client_id, redirect_uri, challenge):
        """
        A one-time authorization code for a new read-only connection. The connection
        stays inactive until the client exchanges the code: a browser closed now
        leaves nothing in the app's list.
        """
        now = self.identity.clock()
        grant_id = secrets.token_urlsafe(16)
        code = secrets.token_urlsafe(32)
        with self.identity.connect() as db:
            self._sweep(db, now)
            db.execute(
                "INSERT INTO oauth_grants (id, user_id, client_id, redirect_uri, scope, created,"
                " expires) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (grant_id, user_id, client_id, redirect_uri, READ, now, now + GRANT_IDLE_TTL),
            )
            db.execute(
                "INSERT INTO oauth_codes (hash, grant_id, client_id, redirect_uri, code_challenge,"
                " expires) VALUES (?, ?, ?, ?, ?, ?)",
                (digest(code), grant_id, client_id, redirect_uri, challenge, now + CODE_TTL),
            )
        return code

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
                "SELECT oauth_codes.*, oauth_grants.scope, oauth_grants.user_id AS owner"
                " FROM oauth_codes JOIN oauth_grants ON oauth_grants.id=oauth_codes.grant_id"
                " WHERE hash=?",
                (digest(form["code"]),),
            ).fetchone()
            if row is not None and row["used"]:
                # A code used twice was stolen by one of the two: end what it gave.
                db.execute("DELETE FROM oauth_grants WHERE id=?", (row["grant_id"],))
            elif (
                row is not None
                and row["expires"] > now
                and row["client_id"] == form["client_id"]
                and row["redirect_uri"] == form["redirect_uri"]
                and VERIFIER.fullmatch(verifier)
                and hmac.compare_digest(s256(verifier), row["code_challenge"])
            ):
                db.execute("UPDATE oauth_codes SET used=1 WHERE hash=?", (row["hash"],))
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
form { display: grid; gap: 14px; margin-top: 20px; }
label { display: grid; gap: 4px; font-weight: 600; }
input { font: inherit; min-height: 48px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; background: var(--card); color: var(--text); }
button { font: inherit; font-weight: 700; min-height: 48px; border: 1px solid var(--line); border-radius: 10px; background: var(--card); color: var(--text); }
button[value="allow"] { border-color: var(--accent); background: var(--accent); color: var(--bg); }
.error { font-weight: 700; color: #d93025; }
"""


def _page(nonce, body):
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{APP_NAME} 에이전트 연결</title>"
        f'<style nonce="{nonce}">{_STYLE}{_PAGE_STYLE}</style></head><body><main>'
        f'<header><span class="app">{APP_NAME}</span><span class="dev">에이전트 연결</span></header>'
        f"{body}</main></body></html>"
    )


def _form_origin(redirect_uri):
    """What the page's CSP form-action names so the redirect after the POST may leave."""
    parts = urlsplit(redirect_uri)
    # CSP host sources cannot name an IPv6 literal; the redirect is still one this server checked.
    if parts.hostname == "::1":
        return "http:"
    return f"{parts.scheme}://{parts.netloc}"


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

    def html(body, status, redirect_uri=None):
        nonce = secrets.token_urlsafe(16)
        # Chrome applies form-action to the redirect that follows a form, too: the
        # client's callback origin must be named or the 302 back to it is blocked.
        form_action = f"'self' {_form_origin(redirect_uri)}" if redirect_uri else "'none'"
        response = Response(_page(nonce, body), status, mimetype="text/html")
        response.headers["Content-Security-Policy"] = (
            f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none';"
            f" form-action {form_action}; frame-ancestors 'none'"
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

    def checked(params):
        """The client behind a valid authorization request, or the response that ends it."""
        client = store.client(params.get("client_id", ""))
        redirect_uri = params.get("redirect_uri", "")
        target = redirect_target(redirect_uri)
        if (
            client is None
            or target is None
            or target not in map(redirect_target, client["redirect_uris"])
        ):
            # Never redirect for a client or a redirect this server cannot vouch for.
            text = "연결 요청을 확인할 수 없어요. 에이전트에서 연결을 다시 시작해 주세요."
            return None, html(f"<p>{text}</p>", 400)
        # A person reads this answer, so it is a page like the others, not OAuth JSON.
        if not (
            identity.allow("oauth-authorize:" + client_address(), 60, 60)
            and identity.allow("oauth-authorize", 300, 60)
        ):
            return None, html("<p>요청이 너무 많아요. 잠시 후 다시 시도해 주세요.</p>", 429)
        error = None
        if params.get("response_type") != "code":
            error = "unsupported_response_type"
        elif params.get("code_challenge_method") != "S256" or not CHALLENGE.fullmatch(
            params.get("code_challenge", "")
        ):
            error = "invalid_request"
        elif params.get("resource", store.resource) != store.resource:
            error = "invalid_target"
        elif "scope" in params and not SCOPE.fullmatch(params["scope"]):
            error = "invalid_scope"
        return (None, back(params, error=error)) if error else (client, None)

    def back(params, **query):
        """The redirect to the client, with its state and this issuer (RFC 9207)."""
        if "state" in params:
            query["state"] = params["state"]
        query["iss"] = public_url
        return redirect(f"{params['redirect_uri']}?{urlencode(query)}", 302)

    def consent(client, params, status=200, username="", error=None):
        """The sign-in page. Each one gets a fresh CSRF value, in a cookie and in the form."""
        csrf = secrets.token_urlsafe(32)
        who = describe_client(client["name"], params["redirect_uri"])
        asker = LOCAL_PROGRAM if who["local"] else who["host"]
        hidden = "".join(
            f'<input type="hidden" name="{key}" value="{escape(params[key])}">'
            for key in REQUEST_FIELDS
            if key in params
        )
        body = (
            f"<p><b>{escape(client['name'])} — {escape(asker)}</b></p>"
            f"<p>이 프로그램이 {APP_NAME} 계정에 연결하려고 해요. "
            f"{APP_NAME} 아이디와 비밀번호로 로그인해 허용해 주세요.</p>"
            '<p class="muted">조회만 허용합니다. 예약은 앱의 설정 → 에이전트 연결에서 켤 수 있어요.</p>'
            + (f'<p class="error" role="alert">{error}</p>' if error else "")
            + '<form method="post" action="/oauth/authorize">'
            f'{hidden}<input type="hidden" name="csrf" value="{csrf}">'
            '<label>아이디<input name="username" autocomplete="username" autocapitalize="none"'
            f' spellcheck="false" maxlength="32" required value="{escape(username)}"></label>'
            '<label>비밀번호<input type="password" name="password" autocomplete="current-password"'
            ' maxlength="128" required></label>'
            '<button type="submit" name="decision" value="allow">로그인하고 허용</button>'
            '<button type="submit" name="decision" value="deny" formnovalidate>거절</button>'
            "</form>"
        )
        response = html(body, status, params["redirect_uri"])
        response.set_cookie(
            cookie,
            csrf,
            max_age=FORM_TTL,
            path="/",
            secure=secure,
            httponly=True,
            samesite="Lax",
        )
        return response

    @app.get("/oauth/authorize")
    def authorize():
        client, ended = checked(request.args)
        return ended or consent(client, request.args)

    @app.post("/oauth/authorize")
    def authorize_decision():
        form = request.form
        sent, kept = form.get("csrf", ""), request.cookies.get(cookie, "")
        if not sent or not hmac.compare_digest(sent.encode(), kept.encode()):
            text = "이 페이지로는 연결할 수 없어요. 에이전트에서 연결을 다시 시작해 주세요."
            return html(f"<p>{text}</p>", 400)
        client, ended = checked(form)
        if ended:
            return ended
        if form.get("decision") != "allow":
            return back(form, error="access_denied")
        username = form.get("username", "")
        username = username if len(username) <= 32 else ""
        # The app login's buckets (api.py auth), so a browser adds no guesses of its own.
        if not (
            identity.allow("auth-any", 60, 300)
            and identity.allow("auth-ip:" + client_address(), 10, 300)
            and identity.allow("auth-user:" + username.lower(), 10, 300)
        ):
            text = "요청이 너무 많아요. 잠시 후 다시 시도해 주세요."
            return consent(client, form, 429, username, text)
        try:
            user = identity.verify(username, form.get("password"))
        except AuthError:
            # Never which of the two: that would tell whether the account exists.
            text = "아이디나 비밀번호를 다시 확인해 주세요."
            return consent(client, form, 401, username, text)
        code = store.allow(user["id"], client["id"], form["redirect_uri"], form["code_challenge"])
        return back(form, code=code)

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
