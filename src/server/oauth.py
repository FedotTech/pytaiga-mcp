"""Sign-in through the browser: TAIGA_AUTH_MODE=oauth.

This server is the OAuth authorization server for MCP clients (the SDK serves
/.well-known/*, /authorize, /token, /register, /revoke); the person signs in on
Taiga's own consent page for a separate external Application
(TAIGA_OAUTH_APP_ID), so the Taiga password never passes through here:

    client -> /authorize -> /oauth/start (cookie) -> Taiga /external-apps?application=..&state=S
    Taiga "Allow" -> /oauth/taiga-callback?auth_code=X (Taiga does not echo S: the cookie finds it)
    -> POST /application-tokens/validate {app, X, S} -> Taiga app token T
    -> redirect_uri?code=..&state=.. -> client /token (PKCE, checked by the SDK)

No storage: issued access/refresh tokens are T and the grant sealed with Fernet
under MCP_OAUTH_SECRET, and a registered client's metadata is sealed into its
client_id. Only half-done sign-ins and one-time codes live in memory (minutes).
Every call reaches Taiga with T through the step-1 verifier (src/server/auth.py),
so deleting the ApplicationToken row (or /revoke) cuts access at once. Personal
keys of step 1 keep working on the same server: load_access_token checks both.
"""

import base64
import hashlib
import json
import logging
import secrets
import time
from typing import Any, Dict, NamedTuple, Optional
from urllib.parse import urlencode

import anyio
import httpx
from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from src.server.auth import AppTokenVerifier
from src.taiga_client import TaigaException, TaigaHTTP

logger = logging.getLogger(__name__)

OAUTH_MODE = "oauth"
START_PATH = "/oauth/start"
CALLBACK_PATH = "/oauth/taiga-callback"
COOKIE = "taiga_mcp_login"
LOGIN_TTL = 600  # seconds to finish the Taiga page
CODE_TTL = 300  # seconds to trade the code at /token
# Every Fernet token of this era starts so (version 0x80 + high timestamp bytes).
# ponytail: a step-1 key starting with it (~1e-11) is just refused.
_SEALED = "gAAAAA"


class _Login(NamedTuple):
    expires: float
    client_id: str
    params: AuthorizationParams
    state: str  # sent to Taiga, fresh per sign-in


# The SDK never renders these: extra fields stay server-side.
class _Code(AuthorizationCode):
    taiga_token: str


class _Refresh(RefreshToken):
    taiga_token: str


class _Access(AccessToken):
    taiga_token: str


def _allowed_redirect(uri: AnyUrl) -> bool:
    """Loopback or an app scheme (cursor://...): Taiga re-consents silently, so a
    web redirect would let anyone's registered client collect a person's code."""
    if uri.scheme == "http":
        return uri.host in ("localhost", "127.0.0.1", "[::1]")
    return uri.scheme not in ("https", "javascript", "data", "file", "vbscript")


class TaigaOAuthProvider:
    def __init__(
        self,
        keys: AppTokenVerifier,
        public_url: str,
        taiga_public_url: str,
        app_id: str,
        secret: str,
        token_ttl_seconds: int,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.keys = keys
        self.public_url = public_url.rstrip("/")
        self.resource_url = self.public_url + "/mcp"
        self.taiga_url = taiga_public_url.rstrip("/").removesuffix("/api/v1")
        self.app_id = app_id
        self.ttl = token_ttl_seconds
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
        self._transport = transport  # tests only
        # ponytail: in-memory, one server process; shared store (Redis) for several.
        self._logins: Dict[str, _Login] = {}
        self._codes: Dict[str, _Code] = {}

    def add_routes(self, server) -> None:
        """The two pages of the Taiga round trip (the SDK serves the OAuth endpoints)."""
        server.custom_route(START_PATH, methods=["GET"])(self.start)
        server.custom_route(CALLBACK_PATH, methods=["GET"])(self.taiga_callback)

    # -- sealing -------------------------------------------------------------

    def _seal(self, kind: str, data: Dict[str, Any]) -> str:
        return self._fernet.encrypt(json.dumps({"k": kind, **data}).encode()).decode()

    def _open(self, blob: str, kind: str, ttl: Optional[int] = None):
        """(payload, issued_at) if `blob` is our sealed `kind` and not older than ttl."""
        try:
            payload = json.loads(self._fernet.decrypt(blob))
            issued = self._fernet.extract_timestamp(blob)
        except (InvalidToken, ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("k") != kind:
            return None
        if ttl is not None and issued + ttl < time.time():
            return None
        return payload, issued

    def _issue(self, client_id: str, taiga_token: str, subject, scopes, resource) -> OAuthToken:
        grant = {"t": taiga_token, "c": client_id, "s": subject, "sc": scopes, "r": resource}
        # ponytail: no rotation record; an old refresh token lives to its own expiry.
        return OAuthToken(
            access_token=self._seal("access", grant),
            expires_in=self.ttl,
            refresh_token=self._seal("refresh", grant),
            scope=" ".join(scopes) or None,
        )

    # -- clients (DCR) --------------------------------------------------------

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        opened = self._open(client_id, "client")
        if opened is None:
            return None
        return OAuthClientInformationFull.model_validate({**opened[0]["m"], "client_id": client_id})

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not _allowed_redirect(uri):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    f"{uri}: only http://localhost / 127.0.0.1 or an app scheme is accepted",
                )
        meta = client_info.model_dump(mode="json", exclude_none=True, exclude={"client_id"})
        # The SDK returns this same object to the client.
        client_info.client_id = self._seal("client", {"m": meta})

    # -- sign-in ----------------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        now = time.time()
        self._logins = {k: v for k, v in self._logins.items() if v.expires > now}
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}
        req = secrets.token_urlsafe(32)
        self._logins[req] = _Login(
            now + LOGIN_TTL, client.client_id, params, secrets.token_urlsafe(32)
        )
        return f"{self.public_url}{START_PATH}?{urlencode({'req': req})}"

    async def start(self, request: Request) -> Response:
        """Bind this browser to the sign-in (cookie), then send it to Taiga's consent page."""
        req = request.query_params.get("req", "")
        login = self._logins.get(req)
        if login is None or login.expires < time.time():
            return _expired()
        query = urlencode({"application": self.app_id, "state": login.state})
        response = RedirectResponse(f"{self.taiga_url}/external-apps?{query}", 302)
        response.set_cookie(
            COOKIE,
            req,
            max_age=LOGIN_TTL,
            path="/oauth",
            httponly=True,
            secure=self.public_url.startswith("https://"),
            samesite="lax",
        )
        return response

    async def taiga_callback(self, request: Request) -> Response:
        login = self._logins.pop(request.cookies.get(COOKIE, ""), None)
        if login is None or login.expires < time.time():
            return _expired()
        auth_code = request.query_params.get("auth_code")
        user = taiga_token = None
        if auth_code:
            try:
                taiga_token = await anyio.to_thread.run_sync(self._validate, auth_code, login.state)
                if taiga_token:
                    user = await self.keys.verify_taiga_token(taiga_token)
            except TaigaException as e:
                logger.warning(f"Taiga refused the sign-in: {type(e).__name__}")
        if user is None:
            return self._back(login, error="access_denied")
        code = secrets.token_urlsafe(32)
        p = login.params
        self._codes[code] = _Code(
            code=code,
            scopes=p.scopes or [],
            expires_at=time.time() + CODE_TTL,
            client_id=login.client_id,
            code_challenge=p.code_challenge,
            redirect_uri=p.redirect_uri,
            redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly,
            resource=p.resource,
            subject=user.subject,
            taiga_token=taiga_token,
        )
        logger.info(f"Browser sign-in for Taiga user id {user.subject}.")
        return self._back(login, code=code)

    def _validate(self, auth_code: str, state: str) -> Optional[str]:
        """Trade Taiga's one-time auth_code for the person's app token (anonymous call)."""
        api = TaigaHTTP(self.keys.host, transport=self._transport)
        try:
            data = api.post(
                "/application-tokens/validate",
                json={"application": self.app_id, "auth_code": auth_code, "state": state},
            )
        finally:
            api.close()
        return data.get("token") if isinstance(data, dict) else None

    def _back(self, login: _Login, **params: str) -> Response:
        url = construct_redirect_uri(
            str(login.params.redirect_uri), state=login.params.state, **params
        )
        response = RedirectResponse(url, 302, headers={"Cache-Control": "no-store"})
        response.delete_cookie(COOKIE, path="/oauth")
        return response

    # -- tokens -----------------------------------------------------------------

    async def load_authorization_code(self, client, authorization_code: str) -> Optional[_Code]:
        return self._codes.get(authorization_code)

    async def exchange_authorization_code(self, client, authorization_code: _Code) -> OAuthToken:
        code = self._codes.pop(authorization_code.code, None)
        if code is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(
            client.client_id, code.taiga_token, code.subject, code.scopes, code.resource
        )

    async def load_refresh_token(self, client, refresh_token: str) -> Optional[_Refresh]:
        opened = self._open(refresh_token, "refresh", self.ttl)
        if opened is None or opened[0]["c"] != client.client_id:
            return None
        p, issued = opened
        return _Refresh(
            token=refresh_token,
            client_id=p["c"],
            scopes=p["sc"],
            expires_at=int(issued + self.ttl),
            resource=p["r"],
            subject=p["s"],
            taiga_token=p["t"],
        )

    async def exchange_refresh_token(self, client, refresh_token: _Refresh, scopes) -> OAuthToken:
        if await self.keys.verify_taiga_token(refresh_token.taiga_token) is None:
            raise TokenError("invalid_grant", "access was revoked in Taiga")
        return self._issue(
            client.client_id,
            refresh_token.taiga_token,
            refresh_token.subject,
            scopes,
            refresh_token.resource,
        )

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        if not token.startswith(_SEALED):
            return await self.keys.verify_token(token)  # a step-1 personal key
        opened = self._open(token, "access", self.ttl)
        if opened is None:
            return None
        p, issued = opened
        if p["r"] and p["r"].rstrip("/") != self.resource_url:
            return None  # issued for another resource (RFC 8707)
        user = await self.keys.verify_taiga_token(p["t"])
        if user is None:
            return None
        return _Access(
            token=token,
            client_id=p["c"],
            scopes=p["sc"],
            expires_at=int(issued + self.ttl),
            resource=p["r"],
            subject=user.subject,
            claims=user.claims,
            taiga_token=p["t"],
        )

    async def revoke_token(self, token) -> None:
        """Delete the person's Taiga app token: every client of theirs is signed out."""
        taiga_token = getattr(token, "taiga_token", None)
        if taiga_token:
            await anyio.to_thread.run_sync(self._revoke, taiga_token)

    def _revoke(self, taiga_token: str) -> None:
        api = TaigaHTTP(self.keys.host, transport=self._transport, auth_scheme="Application")
        api.auth_token = taiga_token
        try:
            rows = api.get("/application-tokens", params={"application": self.app_id}) or []
            for row in rows:
                api.delete(f"/application-tokens/{row['id']}")
            logger.info("Browser sign-in revoked in Taiga.")
        except TaigaException as e:
            logger.warning(f"Revoking in Taiga failed: {type(e).__name__}")
        finally:
            api.close()
        self.keys._forget(taiga_token)


def _expired() -> Response:
    return PlainTextResponse(
        "This sign-in has expired or was opened in another browser. "
        "Start it again from your MCP client.",
        status_code=400,
    )
