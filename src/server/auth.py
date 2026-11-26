"""Personal keys over HTTP: TAIGA_AUTH_MODE=app-token.

Each person gets a random key (scripts/mcp_keys.py). Taiga stores only
sha256(key) as the `token` of an external-app ApplicationToken bound to that
person, so the server calls Taiga with `Authorization: Application <sha256(key)>`
and every action runs as that person, with their rights. The key itself is
stored nowhere; the server keeps no key file.

`AppTokenVerifier` is FastMCP's `token_verifier`: the SDK checks
`Authorization: Bearer <key>` on every HTTP request and answers 401 when the
verifier returns None. A good key gets a personal client in
`core.active_sessions["user:<id>"]`; `core._get_session_id` routes every tool
there. Revocation is immediate: each tool call goes to Taiga with the hash, and
Taiga answers 401 once the token row is deleted. The TTL cache only saves one
GET /users/me per request.
"""

import hashlib
import logging
import time
from typing import Dict, Optional, Tuple

import anyio
import httpx
from mcp.server.auth.provider import AccessToken

from src.server import core
from src.taiga_client import TaigaAuthenticationError, TaigaClientWrapper, TaigaHTTP

logger = logging.getLogger(__name__)

APP_TOKEN_MODE = "app-token"
CLIENT_ID = "pytaiga-mcp"


def key_hash(key: str) -> str:
    """The Taiga token for a key. scripts/mcp_keys.py must compute the same."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class AppTokenVerifier:
    def __init__(
        self,
        host: str,
        ttl_seconds: float = 300,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.host = host
        self.ttl_seconds = ttl_seconds
        self._transport = transport  # tests only
        # {sha256(key): (monotonic expiry, AccessToken)}
        # ponytail: no eviction of stale entries; a handful of people, add a sweep if that grows.
        self._cache: Dict[str, Tuple[float, AccessToken]] = {}

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if not token:
            return None
        return await self.verify_taiga_token(key_hash(token), token)

    async def verify_taiga_token(
        self, h: str, token: Optional[str] = None
    ) -> Optional[AccessToken]:
        """Check the Taiga app token `h` itself (browser sign-in, src/server/oauth.py)."""
        token = token or h
        hit = self._cache.get(h)
        if hit and hit[0] > time.monotonic() and self._session_holds(hit[1].subject, h):
            return hit[1]
        return await anyio.to_thread.run_sync(self._check, token, h)

    @staticmethod
    def _session_holds(subject: Optional[str], h: str) -> bool:
        """The user's session exists and calls Taiga with this hash (not logged out)."""
        wrapper = core.active_sessions.get(core._user_session_id(subject))
        return bool(wrapper and wrapper.is_authenticated and wrapper.api.auth_token == h)

    def _check(self, token: str, h: str) -> Optional[AccessToken]:
        api = TaigaHTTP(self.host, transport=self._transport, auth_scheme="Application")
        api.auth_token = h
        try:
            me = api.get("/users/me")
        except TaigaAuthenticationError:
            api.close()
            self._forget(h)
            return None
        except BaseException:
            # Timeout or 5xx: not a verdict on the key. The SDK answers 500, not 401,
            # so the client does not conclude its key is wrong.
            api.close()
            raise
        # Taiga does not check is_active for app tokens (external_apps/auth_backends.py).
        if not isinstance(me, dict) or me.get("id") is None or me.get("is_active") is False:
            api.close()
            self._forget(h)
            logger.warning("Personal key refused: its Taiga user is inactive or unknown.")
            return None

        subject = str(me["id"])
        session_id = core._user_session_id(subject)
        if self._session_holds(subject, h):
            api.close()  # keep the live client and its lookup cache
        else:
            wrapper = TaigaClientWrapper(self.host)
            wrapper.api = api
            old = core.active_sessions.get(session_id)
            core.active_sessions[session_id] = wrapper
            if old is not None:
                old.close()  # a re-issued key: the old one no longer works anyway
            logger.info(f"Personal session opened for Taiga user id {subject}.")
        access = AccessToken(
            token=token,
            client_id=CLIENT_ID,
            scopes=[],
            subject=subject,
            claims={"username": me.get("username")},
        )
        self._cache[h] = (time.monotonic() + self.ttl_seconds, access)
        return access

    def _forget(self, h: str) -> None:
        """Drop the cache entry and the user's session if it still runs on this hash."""
        hit = self._cache.pop(h, None)
        if hit is None:
            return
        session_id = core._user_session_id(hit[1].subject)
        wrapper = core.active_sessions.get(session_id)
        if wrapper is not None and wrapper.api is not None and wrapper.api.auth_token == h:
            core._drop_session(session_id)
            core._clear_session_cache(session_id)
