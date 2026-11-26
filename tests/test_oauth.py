"""Browser sign-in (TAIGA_AUTH_MODE=oauth): src/server/oauth.py through the real SDK routes."""

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from pydantic import SecretStr
from starlette.testclient import TestClient

from src.server import app, auth, core, oauth

PUBLIC = "http://localhost:8000"
TAIGA = "https://taiga.example"
REDIRECT = "http://localhost:5555/callback"
ALICE = {"id": 7, "username": "alice", "is_active": True}


class FakeTaiga:
    """external_apps as Taiga 6.10 has it: validate (anonymous), users/me, list/delete own token."""

    def __init__(self):
        self.users = {}  # app token -> user (step-1 hashes too)
        self.grants = {}  # auth_code -> (state, app token)
        self.calls = []

    def consent(self, state, token="T-alice", user=ALICE):
        """What 'Allow' on /external-apps does; returns the auth_code Taiga puts in next_url."""
        code = secrets.token_hex(8)
        self.grants[code] = (state, token)
        self.users[token] = user
        return code

    def __call__(self, request):
        path, auth_header = request.url.path, request.headers.get("authorization", "")
        self.calls.append((request.method, path))
        token = auth_header.removeprefix("Application ") if auth_header else None
        if path == "/api/v1/application-tokens/validate":
            data = json.loads(request.content)
            state, tok = self.grants.pop(data["auth_code"], (None, None))
            if data["application"] != "pytaiga-mcp-oauth" or state != data["state"]:
                return httpx.Response(404, json={"_error_message": "No ApplicationToken"})
            return httpx.Response(200, json={"token": tok})
        if token not in self.users:
            return httpx.Response(401, json={"_error_message": "Invalid token"})
        if path == "/api/v1/users/me":
            return httpx.Response(200, json=self.users[token])
        if path == "/api/v1/application-tokens" and request.method == "GET":
            return httpx.Response(200, json=[{"id": 1}])
        if path == "/api/v1/application-tokens/1" and request.method == "DELETE":
            del self.users[token]
            return httpx.Response(204)
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def clean_sessions(monkeypatch):
    core.active_sessions.clear()
    core._session_cache.clear()
    monkeypatch.setattr(core, "personal_auth", True)
    yield
    core.active_sessions.clear()
    core._session_cache.clear()


@pytest.fixture
def setup(monkeypatch):
    """The server exactly as app._auth_kwargs builds it, with Taiga faked."""
    monkeypatch.setattr(app.settings, "auth_mode", "oauth")
    monkeypatch.setattr(app.settings, "public_url", PUBLIC)
    monkeypatch.setattr(app.settings, "taiga_public_url", TAIGA)
    monkeypatch.setattr(app.settings, "oauth_secret", SecretStr("s" * 32))
    taiga = FakeTaiga()
    kwargs = app._auth_kwargs()
    provider = kwargs["auth_server_provider"]
    provider._transport = provider.keys._transport = httpx.MockTransport(taiga)
    server = FastMCP("t", stateless_http=True, json_response=True, port=8000, **kwargs)
    provider.add_routes(server)

    @server.tool()
    def whoami() -> str:
        return core._get_session_id()

    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as client:
        yield client, taiga, provider


def _pkce():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _register(client, redirect=REDIRECT):
    return client.post(
        "/register",
        json={
            "redirect_uris": [redirect],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )


def _sign_in(client, taiga, verifier=None, challenge=None):
    """Client -> /authorize -> /oauth/start -> (Taiga consent) -> callback -> code."""
    if verifier is None:
        verifier, challenge = _pkce()
    client_id = _register(client).json()["client_id"]
    r = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "state": "cs",
            "resource": PUBLIC + "/mcp",
        },
        follow_redirects=False,
    )
    start = urlparse(r.headers["location"])
    assert start.path == oauth.START_PATH
    r = client.get(f"{start.path}?{start.query}", follow_redirects=False)
    to_taiga = urlparse(r.headers["location"])
    assert f"{to_taiga.scheme}://{to_taiga.netloc}{to_taiga.path}" == TAIGA + "/external-apps"
    q = parse_qs(to_taiga.query)
    assert q["application"] == ["pytaiga-mcp-oauth"]
    auth_code = taiga.consent(q["state"][0])
    r = client.get(oauth.CALLBACK_PATH, params={"auth_code": auth_code}, follow_redirects=False)
    back = urlparse(r.headers["location"])
    assert back.netloc == "localhost:5555"
    return client_id, verifier, parse_qs(back.query)


def _token(client, client_id, **form):
    return client.post("/token", data={"client_id": client_id, **form})


def _call_whoami(client, token):
    return client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        },
    )


def test_full_flow_token_refresh_tool_call_and_revoke(setup):
    client, taiga, _ = setup
    client_id, verifier, back = _sign_in(client, taiga)
    assert back["state"] == ["cs"]
    r = _token(
        client,
        client_id,
        grant_type="authorization_code",
        code=back["code"][0],
        redirect_uri=REDIRECT,
        code_verifier=verifier,
    )
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert tokens["expires_in"] == 90 * 86400
    assert "T-alice" not in tokens["access_token"]  # sealed, not the Taiga token

    # The code is single-use.
    again = _token(
        client,
        client_id,
        grant_type="authorization_code",
        code=back["code"][0],
        redirect_uri=REDIRECT,
        code_verifier=verifier,
    )
    assert again.status_code == 400

    r = _call_whoami(client, tokens["access_token"])
    assert r.status_code == 200 and "user:7" in r.text
    assert core.active_sessions["user:7"].api.auth_token == "T-alice"

    r = _token(client, client_id, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert r.status_code == 200, r.text
    assert "user:7" in _call_whoami(client, r.json()["access_token"]).text

    # mcp 1.30 RevocationRequest requires the client_secret field even for public clients.
    r = client.post(
        "/revoke",
        data={"client_id": client_id, "client_secret": "", "token": tokens["refresh_token"]},
    )
    assert r.status_code == 200, r.text
    assert ("DELETE", "/api/v1/application-tokens/1") in taiga.calls
    assert "T-alice" not in taiga.users
    assert _call_whoami(client, tokens["access_token"]).status_code == 401
    r = _token(client, client_id, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
    assert r.status_code == 400  # revoked in Taiga: no new tokens


def test_401_points_at_oauth_metadata(setup):
    client, _, _ = setup
    r = client.post("/mcp", json={})
    assert r.status_code == 401
    assert "/.well-known/oauth-protected-resource/mcp" in r.headers["www-authenticate"]
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"].endswith("/register")
    assert meta["code_challenge_methods_supported"] == ["S256"]


def test_wrong_pkce_verifier_refused(setup):
    client, taiga, _ = setup
    client_id, _, back = _sign_in(client, taiga)
    r = _token(
        client,
        client_id,
        grant_type="authorization_code",
        code=back["code"][0],
        redirect_uri=REDIRECT,
        code_verifier="x" * 50,
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_callback_without_cookie_or_with_stale_state(setup):
    client, taiga, provider = setup
    verifier, challenge = _pkce()
    client_id = _register(client).json()["client_id"]
    r = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
        },
        follow_redirects=False,
    )
    start = urlparse(r.headers["location"])
    r = client.get(f"{start.path}?{start.query}", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    cookie = client.cookies.get(oauth.COOKIE)

    client.cookies.clear()  # another browser
    r = client.get(oauth.CALLBACK_PATH, params={"auth_code": taiga.consent(state)})
    assert r.status_code == 400

    client.cookies.set(oauth.COOKIE, cookie)
    taiga.grants.clear()
    stale = taiga.consent("some-older-state")  # Taiga's code for another sign-in
    r = client.get(oauth.CALLBACK_PATH, params={"auth_code": stale}, follow_redirects=False)
    assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["access_denied"]
    assert provider._logins == {}  # used up either way


def test_personal_key_still_works_and_garbage_is_401(setup):
    client, taiga, _ = setup
    taiga.users[auth.key_hash("console-key")] = {"id": 8, "username": "bob", "is_active": True}
    assert "user:8" in _call_whoami(client, "console-key").text
    assert _call_whoami(client, "gAAAAAforged-ciphertext").status_code == 401
    assert _call_whoami(client, "nope").status_code == 401


def test_tampered_or_foreign_token_refused(setup, monkeypatch):
    client, taiga, provider = setup
    client_id, verifier, back = _sign_in(client, taiga)
    tokens = _token(
        client,
        client_id,
        grant_type="authorization_code",
        code=back["code"][0],
        redirect_uri=REDIRECT,
        code_verifier=verifier,
    ).json()
    access = tokens["access_token"]
    flipped = access[:-5] + ("A" if access[-5] != "A" else "B") + access[-4:]
    assert _call_whoami(client, flipped).status_code == 401
    # A refresh token or a client_id is not an access token.
    assert _call_whoami(client, tokens["refresh_token"]).status_code == 401
    assert _call_whoami(client, client_id).status_code == 401
    # Expired after the TTL.
    now = oauth.time.time()
    monkeypatch.setattr(oauth.time, "time", lambda: now + 90 * 86400 + 1)
    assert _call_whoami(client, access).status_code == 401


def test_registration_only_for_loopback_or_app_schemes(setup):
    client, _, _ = setup
    assert _register(client, "http://127.0.0.1:33418/callback").status_code == 201
    assert _register(client, "cursor://anysphere.cursor-mcp/oauth/callback").status_code == 201
    r = _register(client, "https://evil.example/cb")
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
    assert _register(client, "http://evil.example/cb").status_code == 400


class TestModeSwitch:
    def test_oauth_needs_public_url_and_secret(self, monkeypatch):
        monkeypatch.setattr(app.settings, "auth_mode", "oauth")
        monkeypatch.setattr(app.settings, "username", None)
        monkeypatch.setattr(app.settings, "password", None)
        monkeypatch.setattr(app.settings, "public_url", "")
        with pytest.raises(SystemExit, match="MCP_PUBLIC_URL"):
            app._set_personal_auth("streamable-http")
        monkeypatch.setattr(app.settings, "public_url", PUBLIC)
        monkeypatch.setattr(app.settings, "oauth_secret", None)
        with pytest.raises(SystemExit, match="MCP_OAUTH_SECRET"):
            app._set_personal_auth("streamable-http")
        assert app._auth_kwargs() == {}


def test_console_script_matches_server_defaults():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "mcp_keys.py"
    spec = importlib.util.spec_from_file_location("mcp_keys", path)
    mcp_keys = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp_keys)
    assert mcp_keys.OAUTH_APP_ID == type(app.settings).model_fields["oauth_app_id"].default
    assert "/oauth/taiga-callback" == oauth.CALLBACK_PATH
