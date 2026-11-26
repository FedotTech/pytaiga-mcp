"""Issue, list and revoke personal pytaiga-mcp keys (TAIGA_AUTH_MODE=app-token).

Runs inside the taiga-back container, fed to `python manage.py shell` on stdin
by scripts/mcp-keys.sh, which passes the command in MCP_KEYS_ARGS:

    issue <username|email>   new key, printed once; replaces the person's old key
    list                     who has a key; flags inactive users still holding one
    revoke <username|email>  delete the person's key

Taiga stores only sha256(key) as an external-app token; the key itself is
stored nowhere. Taiga does NOT check is_active for app tokens: when someone
leaves, run `revoke` first, then deactivate the account.
"""

import hashlib
import os
import secrets
import sys

APP_ID = "pytaiga-mcp"


def key_hash(key):
    # Must match src/server/auth.py:key_hash (tests/test_auth.py checks both).
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _user(identifier):
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    users = list(
        get_user_model().objects.filter(
            Q(username=identifier) | Q(email__iexact=identifier), is_system=False
        )
    )
    if len(users) != 1:
        sys.exit(f"No single (non-system) Taiga user '{identifier}'.")
    return users[0]


def issue(identifier):
    from taiga.external_apps.models import Application, ApplicationToken

    user = _user(identifier)
    if not user.is_active:
        sys.exit(f"User '{user.username}' is inactive: no key issued.")
    app, _ = Application.objects.get_or_create(
        id=APP_ID, defaults={"name": APP_ID, "next_url": "urn:none"}
    )
    key = secrets.token_urlsafe(32)
    _, created = ApplicationToken.objects.update_or_create(
        application=app,
        user=user,
        defaults={"token": key_hash(key), "auth_code": None, "state": ""},
    )
    print(f"Key for {user.username} (save it now, it cannot be shown again):")
    print(key)
    if not created:
        print("The previous key of this user no longer works.")


def list_keys():
    from taiga.external_apps.models import ApplicationToken

    tokens = ApplicationToken.objects.filter(application_id=APP_ID).select_related("user")
    inactive = []
    for t in tokens.order_by("user__username"):
        u = t.user
        flag = "" if u.is_active else "   <-- INACTIVE, revoke this key"
        print(f"{t.id:>5}  {u.username:<24} {u.get_full_name():<32} active={u.is_active}{flag}")
        if not u.is_active:
            inactive.append(u.username)
    if not tokens.exists():
        print("No keys issued.")
    if inactive:
        print(
            f"\nWARNING: inactive users still hold a working key: {', '.join(inactive)}. "
            "Taiga does not block them. Run: mcp-keys.sh revoke <username>"
        )
    print("\nWhen someone leaves: revoke the key first, then deactivate the account.")


def revoke(identifier):
    from taiga.external_apps.models import ApplicationToken

    user = _user(identifier)
    deleted, _ = ApplicationToken.objects.filter(application_id=APP_ID, user=user).delete()
    print(f"Key of {user.username} revoked." if deleted else f"{user.username} had no key.")


def main(args):
    if len(args) == 2 and args[0] == "issue":
        issue(args[1])
    elif args == ["list"]:
        list_keys()
    elif len(args) == 2 and args[0] == "revoke":
        revoke(args[1])
    else:
        sys.exit("usage: mcp-keys.sh issue <username> | list | revoke <username>")


# Only when fed by mcp-keys.sh; importing it (tests) runs nothing.
if "MCP_KEYS_ARGS" in os.environ:
    main(os.environ["MCP_KEYS_ARGS"].split())
