"""The FastMCP instance, its lifespan (env auto-auth), transport selection and main()."""

import functools
import inspect
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

import anyio
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP

from src import taiga_client
from src.config import settings
from src.server import auth, core, oauth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------

VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")


def _resolve_transport(argv=None, env=None) -> str:
    if argv is None:
        argv = sys.argv
    if env is None:
        env = dict(os.environ)
    if "--sse" in argv:
        return "sse"
    if "--streamable-http" in argv:
        return "streamable-http"
    env_transport = env.get("TAIGA_TRANSPORT", "").lower()
    if env_transport in VALID_TRANSPORTS:
        return env_transport
    if env_transport:
        logger.warning(
            f"Unknown TAIGA_TRANSPORT '{env_transport}', falling back to stdio. "
            f"Valid: {', '.join(VALID_TRANSPORTS)}"
        )
    return "stdio"


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[None]:
    if core.personal_auth:
        logger.info(f"Personal sign-in (TAIGA_AUTH_MODE={settings.auth_mode}): no shared account.")
    elif settings.has_credentials:
        logger.info("Environment credentials detected. Attempting auto-authentication...")
        try:
            wrapper = taiga_client.TaigaClientWrapper(host=settings.host)
            if wrapper.login(
                username=settings.get_username_value(),
                password=settings.get_password_value(),
            ):
                core.active_sessions[core.DEFAULT_SESSION_ID] = wrapper
                core.auto_login_error = None
                logger.info("Auto-authentication successful.")
        except Exception as e:
            core.auto_login_error = f"{type(e).__name__}: {e} (host {settings.host})"
            logger.error(f"Auto-authentication error: {e}")
            logger.warning("Continuing without auto-authentication.")
    else:
        logger.info("No environment credentials. Use login() tool.")
    try:
        yield
    finally:
        logger.info("Server shutting down — clearing sessions.")
        for wrapper in {id(w): w for w in list(core.active_sessions.values())}.values():
            wrapper.close()
        core.active_sessions.clear()
        core._session_cache.clear()


_mcp_port_str = os.environ.get("MCP_PORT", "8000")
try:
    _mcp_port = int(_mcp_port_str)
except ValueError:
    logger.error(f"Invalid MCP_PORT '{_mcp_port_str}', falling back to 8000.")
    _mcp_port = 8000


def _oauth_problem() -> str:
    """Why TAIGA_AUTH_MODE=oauth cannot start, or ''."""
    if not settings.public_url:
        return "TAIGA_AUTH_MODE=oauth: set MCP_PUBLIC_URL (e.g. https://mcp.example.com)."
    secret = settings.oauth_secret.get_secret_value() if settings.oauth_secret else ""
    if len(secret) < 32:
        return "TAIGA_AUTH_MODE=oauth: MCP_OAUTH_SECRET needs 32+ chars (openssl rand -base64 32)."
    if settings.oauth_token_ttl_days < 1:
        return "TAIGA_OAUTH_TOKEN_TTL_DAYS must be 1 or more."
    return ""


def _auth_kwargs() -> dict:
    """FastMCP auth for TAIGA_AUTH_MODE=app-token (src/server/auth.py) or oauth (oauth.py).

    app-token: resource_server_url=None on purpose: the SDK then publishes no
    /.well-known/oauth-protected-resource and its 401 carries no resource_metadata,
    so nothing points a client at an OAuth login. issuer_url is required by
    AuthSettings but unused without an auth server provider.

    oauth: this server is the authorization server; personal keys are still
    accepted (one load_access_token checks both). An incomplete config gives {}
    here and main() refuses to start.
    """
    keys = auth.AppTokenVerifier(settings.host, settings.key_ttl_seconds)
    if settings.auth_mode == auth.APP_TOKEN_MODE:
        return {
            "token_verifier": keys,
            "auth": AuthSettings(issuer_url=settings.host, resource_server_url=None),
        }
    if settings.auth_mode != oauth.OAUTH_MODE or _oauth_problem():
        return {}
    provider = oauth.TaigaOAuthProvider(
        keys,
        settings.public_url,
        settings.taiga_public_url or settings.host,
        settings.oauth_app_id,
        settings.oauth_secret.get_secret_value(),
        settings.oauth_token_ttl_days * 86400,
    )
    return {
        "auth_server_provider": provider,
        "auth": AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=provider.resource_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
            validate_token_resource=False,  # load_access_token checks it (keys have none)
        ),
    }


_auth = _auth_kwargs()
mcp = FastMCP(
    "Taiga",
    dependencies=["httpx"],
    lifespan=server_lifespan,
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=_mcp_port,
    **_auth,
)
if "auth_server_provider" in _auth:
    _auth["auth_server_provider"].add_routes(mcp)


def tool(*args, **kwargs):
    """Register a sync tool on `mcp`, run off the event loop in a worker thread.

    FastMCP calls a sync tool directly on its event loop
    (mcp/server/fastmcp/utilities/func_metadata.py), so one slow Taiga call
    (an unpaginated list, a 30 s timeout) would block every other client of a
    shared HTTP server. Registering an async wrapper that hands the body to
    `anyio.to_thread.run_sync` keeps the loop free.

    - Same tool surface: `functools.wraps` sets `__wrapped__`, and FastMCP
      builds the schema with `inspect.signature`, which follows it.
    - Returns the ORIGINAL sync function, so modules and tests keep calling
      tools synchronously.
    - Thread safety: `httpx.Client` is thread-safe, token refresh is locked
      (TaigaHTTP.refresh), and the shared dicts (active_sessions,
      _session_cache) only see single-key get/set/pop. Two concurrent misses
      on the same cache key may both fetch it; the reads are idempotent.
      Closing a session's client defers until its in-flight requests end
      (TaigaHTTP.close), and _cached never stores into a cache a concurrent
      logout has cleared.
    - Capacity: calls share anyio's default pool of 40 worker threads. A
      cancelled or disconnected call keeps its thread until Taiga answers
      (bounded by the 30 s client timeout). Raise the limit via
      anyio.to_thread.current_default_thread_limiter() if a shared HTTP
      deployment ever needs more concurrency.
    """
    register = mcp.tool(*args, **kwargs)

    def decorator(fn):
        if inspect.iscoroutinefunction(fn):
            # Already async: offloading would hand run_sync a coroutine function
            # and return an un-awaited coroutine as the tool result.
            register(fn)
            return fn

        @functools.wraps(fn)
        async def run_in_worker_thread(*call_args, **call_kwargs):
            return await anyio.to_thread.run_sync(functools.partial(fn, *call_args, **call_kwargs))

        register(run_in_worker_thread)
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _warn_legacy_server_mode(env=None) -> None:
    """Warn when a deployment still sets the pre-v3 TAIGA_SERVER_MODE.

    v3 has one server, so the variable no longer selects anything. Start anyway
    instead of failing: a stack still pinned to `full` keeps running (on the
    single server's tools) and the log says why its CRUD tools are gone.
    """
    mode = (os.environ if env is None else env).get("TAIGA_SERVER_MODE", "").strip().lower()
    if mode and mode != "workflow":
        logger.warning(
            f"TAIGA_SERVER_MODE={mode!r} is ignored: the full CRUD server was removed in v3. "
            "Starting the single Taiga MCP server. Unset TAIGA_SERVER_MODE to silence this."
        )


def _set_personal_auth(transport: str) -> None:
    """Turn on per-key sessions for HTTP; refuse a config that would run open."""
    mode = settings.auth_mode
    if mode and mode not in (auth.APP_TOKEN_MODE, oauth.OAUTH_MODE):
        # A typo must not leave an internet-facing server on the shared account.
        sys.exit(
            f"Unknown TAIGA_AUTH_MODE={mode!r}. "
            f"Valid: {auth.APP_TOKEN_MODE!r}, {oauth.OAUTH_MODE!r} or unset."
        )
    if not mode:
        return
    if transport == "stdio":
        logger.warning("TAIGA_AUTH_MODE is ignored on stdio: the .env account is used.")
        return
    if settings.has_credentials:
        sys.exit(
            f"TAIGA_AUTH_MODE={mode}: unset TAIGA_USERNAME/TAIGA_PASSWORD. "
            "Each person connects as themselves; there is no shared account."
        )
    if mode == oauth.OAUTH_MODE and _oauth_problem():
        sys.exit(_oauth_problem())
    core.personal_auth = True


def main() -> None:
    _warn_legacy_server_mode()
    transport = _resolve_transport()
    _set_personal_auth(transport)
    logger.info(f"Starting Taiga MCP server with {transport} transport")
    mcp.run(transport=transport)
