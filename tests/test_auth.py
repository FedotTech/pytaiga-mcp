"""Personal keys (TAIGA_AUTH_MODE=app-token): src/server/auth.py and its hooks in core/app."""

import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

import src.server as wf
from src.server import app, auth, core
from src.taiga_client import TaigaAuthenticationError, TaigaHTTP

HOST = "https://taiga.example"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_sessions():
    core.active_sessions.clear()
    core._session_cache.clear()
    yield
    core.active_sessions.clear()
    core._session_cache.clear()


class FakeTaiga:
    """/users/me by Application token: {sha256(key): user}; anything else is 401."""

    def __init__(self, users):
        self.users = users
        self.calls = []

    def __call__(self, request):
        auth_header = request.headers.get("authorization", "")
        self.calls.append((request.url.path, auth_header))
        user = self.users.get(auth_header.removeprefix("Application "))
        if not auth_header.startswith("Application ") or user is None:
            return httpx.Response(401, json={"_error_message": "Invalid token"})
        return httpx.Response(200, json=user)


def _verifier(taiga, ttl=300):
    return auth.AppTokenVerifier(HOST, ttl, transport=httpx.MockTransport(taiga))


def _verify(verifier, key):
    return asyncio.run(verifier.verify_token(key))


ALICE = {"id": 7, "username": "alice", "is_active": True}
BOB = {"id": 8, "username": "bob", "is_active": True}


class TestVerifier:
    def test_good_key_sends_its_hash_not_the_key(self):
        taiga = FakeTaiga({auth.key_hash("k-alice"): ALICE})
        token = _verify(_verifier(taiga), "k-alice")
        assert token.subject == "7" and token.client_id == auth.CLIENT_ID
        assert taiga.calls == [("/api/v1/users/me", f"Application {auth.key_hash('k-alice')}")]
        wrapper = core.active_sessions["user:7"]
        assert wrapper.api.auth_scheme == "Application"
        assert wrapper.api.auth_token == auth.key_hash("k-alice")

    def test_wrong_key_is_none(self):
        taiga = FakeTaiga({auth.key_hash("k-alice"): ALICE})
        assert _verify(_verifier(taiga), "nope") is None
        assert _verify(_verifier(taiga), "") is None
        assert core.active_sessions == {}

    def test_inactive_user_refused(self):
        taiga = FakeTaiga({auth.key_hash("k"): {**ALICE, "is_active": False}})
        assert _verify(_verifier(taiga), "k") is None
        assert core.active_sessions == {}

    def test_ttl_cache_then_recheck_and_revoke(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
        taiga = FakeTaiga({auth.key_hash("k"): ALICE})
        v = _verifier(taiga, ttl=300)
        assert _verify(v, "k").subject == "7"
        core._session_cache_for("user:7")["members_1"] = ["x"]
        now[0] += 299
        assert _verify(v, "k").subject == "7"
        assert len(taiga.calls) == 1  # within TTL: no Taiga call
        assert core._session_cache["user:7"] == {"members_1": ["x"]}  # same session kept
        now[0] += 2
        assert _verify(v, "k").subject == "7"
        assert len(taiga.calls) == 2  # after TTL: asked again
        taiga.users.clear()  # revoked in Taiga
        now[0] += 301
        assert _verify(v, "k") is None
        assert "user:7" not in core.active_sessions
        assert "user:7" not in core._session_cache

    def test_logout_forces_a_new_session_within_ttl(self):
        taiga = FakeTaiga({auth.key_hash("k"): ALICE})
        v = _verifier(taiga)
        _verify(v, "k")
        core._drop_session("user:7")
        assert _verify(v, "k").subject == "7"
        assert core.active_sessions["user:7"].is_authenticated

    def test_reissued_key_replaces_session_and_old_key_dies_without_killing_it(self, monkeypatch):
        now = [0.0]
        monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
        taiga = FakeTaiga({auth.key_hash("old"): ALICE})
        v = _verifier(taiga)
        _verify(v, "old")
        taiga.users = {auth.key_hash("new"): ALICE}  # issue again: old row replaced
        _verify(v, "new")
        assert core.active_sessions["user:7"].api.auth_token == auth.key_hash("new")
        now[0] += 1000
        assert _verify(v, "old") is None
        assert core.active_sessions["user:7"].api.auth_token == auth.key_hash("new")

    def test_transient_failure_is_not_a_401(self):
        def down(request):
            return httpx.Response(502, text="bad gateway")

        with pytest.raises(Exception, match="502"):
            _verify(_verifier(down), "k")

    def test_two_keys_two_clients_two_caches(self):
        taiga = FakeTaiga({auth.key_hash("a"): ALICE, auth.key_hash("b"): BOB})
        v = _verifier(taiga)
        _verify(v, "a"), _verify(v, "b")
        a, b = core.active_sessions["user:7"], core.active_sessions["user:8"]
        assert a is not b and a.api.auth_token != b.api.auth_token
        assert core._session_cache_for("user:7") is not core._session_cache_for("user:8")


def test_application_scheme_never_refreshes_on_401():
    taiga = FakeTaiga({})
    api = TaigaHTTP(HOST, transport=httpx.MockTransport(taiga), auth_scheme="Application")
    api.auth_token = "h"
    with pytest.raises(TaigaAuthenticationError):
        api.get("/users/me")
    assert taiga.calls == [("/api/v1/users/me", "Application h")]  # no /auth/refresh


def _as(subject):
    token = AccessToken(token="k", client_id=auth.CLIENT_ID, scopes=[], subject=subject)
    return auth_context_var.set(AuthenticatedUser(token))


class TestSessionRouting:
    def test_key_owner_session_ignores_passed_session_id(self, monkeypatch):
        monkeypatch.setattr(core, "personal_auth", True)
        core.active_sessions["default"] = object()
        reset = _as("7")
        try:
            assert core._get_session_id() == "user:7"
            assert core._get_session_id("user:8") == "user:7"
            assert core._get_session_id("default") == "user:7"
        finally:
            auth_context_var.reset(reset)

    def test_no_key_is_an_error_not_default(self, monkeypatch):
        monkeypatch.setattr(core, "personal_auth", True)
        core.active_sessions["default"] = object()
        with pytest.raises(PermissionError, match="Personal key required"):
            core._get_session_id()

    def test_login_and_default_session_refused(self, monkeypatch):
        monkeypatch.setattr(core, "personal_auth", True)
        with pytest.raises(ValueError, match="personal key"):
            wf.login(host=HOST, username="u", password="p")
        with pytest.raises(ValueError, match="personal key"):
            wf.get_default_session()


class TestModeSwitch:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.setattr(app.settings, "auth_mode", "")
        app._set_personal_auth("streamable-http")
        assert core.personal_auth is False
        assert app._auth_kwargs() == {}

    def test_http_turns_it_on(self, monkeypatch):
        monkeypatch.setattr(core, "personal_auth", False)
        monkeypatch.setattr(app.settings, "auth_mode", "app-token")
        monkeypatch.setattr(app.settings, "username", None)
        monkeypatch.setattr(app.settings, "password", None)
        app._set_personal_auth("streamable-http")
        assert core.personal_auth is True
        kwargs = app._auth_kwargs()
        assert isinstance(kwargs["token_verifier"], auth.AppTokenVerifier)
        assert kwargs["auth"].resource_server_url is None  # no OAuth metadata route

    def test_stdio_unchanged(self, monkeypatch):
        monkeypatch.setattr(core, "personal_auth", False)
        monkeypatch.setattr(app.settings, "auth_mode", "app-token")
        app._set_personal_auth("stdio")
        assert core.personal_auth is False

    def test_typo_and_shared_account_refuse_to_start(self, monkeypatch):
        monkeypatch.setattr(app.settings, "auth_mode", "apptoken")
        with pytest.raises(SystemExit):
            app._set_personal_auth("streamable-http")
        monkeypatch.setattr(app.settings, "auth_mode", "app-token")
        monkeypatch.setattr(app.settings, "username", "svc")
        monkeypatch.setattr(app.settings, "password", "pw")
        with pytest.raises(SystemExit):
            app._set_personal_auth("streamable-http")


def test_key_hash_matches_issuing_script():
    spec = importlib.util.spec_from_file_location("mcp_keys", ROOT / "scripts" / "mcp_keys.py")
    mcp_keys = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcp_keys)
    vector = "abc"  # FIPS 180-2 test vector
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert auth.key_hash(vector) == mcp_keys.key_hash(vector)
    assert auth.key_hash(vector) == expected
