<p align="center">
<picture>
  <img src="https://taiga.io/media/images/favicon.width-44.png">
</picture>
</p>

# Taiga MCP Bridge

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![GHCR](https://img.shields.io/badge/ghcr.io-tetra--2023%2Fpytaiga--mcp-blue?logo=docker)](https://ghcr.io/tetra-2023/pytaiga-mcp)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

> Originally forked from [talhaorak/pytaiga-mcp](https://github.com/talhaorak/pytaiga-mcp), now an independent project maintained by TETRA.

An [MCP](https://modelcontextprotocol.io/) server that lets AI assistants work in [Taiga](https://taiga.io/): plan sprints, groom the backlog, break stories into tasks, read and write comments, manage attachments, tags and project settings.

**One server, 59 intent tools.** Every tool takes the project as a slug or ID and items by their **ref number** (the `#42` you see in Taiga), and accepts statuses, sprints, roles and people by **name** (member email or full name), resolving them internally. Composite reads answer a question in one call: `get_sprint_board` returns the sprint with every story and task; `plan_sprint` creates a sprint and fills it.

> **Upgrading from v2?** v3 removed the 107-tool `full` CRUD mode and `TAIGA_SERVER_MODE`. The capabilities that were kept came back as intent tools (below). A container that still sets `TAIGA_SERVER_MODE` starts normally and logs a warning. The `mcp-taiga-workflow` command still works as an alias of `mcp-taiga` for the v3.x line.

## Tools

| Group | Tools |
|---|---|
| **Session** | `get_default_session`, `login`, `logout`, `session_status`, `get_current_user` |
| **Projects** | `list_projects`, `get_project_overview`, `search`, `get_team_workload`, `get_project_health`, `get_project_activity` |
| **Stories** | `get_story` (with every task of the story, closed ones included, and counts by status; tasks=False for the counts only; over 50k characters it is refused, never cut), `browse_backlog` (stories in no sprint), `create_story`, `update_story`, `set_story_status` |
| **Tasks** | `get_task`, `create_task`, `update_task`, `break_down_story`, `set_task_status` |
| **Issues** | `get_issue`, `create_issue`, `update_issue` |
| **Sprints** | `get_sprint_board`, `plan_sprint`, `move_to_sprint`, `close_sprint` |
| **Epics** | `get_epic_overview` (stories=False, or sections=[...] for chosen description sections; over 50k characters it is refused, never cut), `create_epic`, `update_epic`, `link_to_epic` |
| **Any item** | `list_items` (every story — sprint-assigned included —, task, issue, epic or sprint, with filters: a status list, blocked, modified_before, and for issues type, priority and severity; over 50k characters it is refused with a breakdown, never cut; `history=True` adds status_since and blocked_since to story, task and issue rows, up to 100 rows), `assign_item`, `delete_item` |
| **Comments & history** | `add_comment`, `list_comments`, `edit_comment`, `delete_comment`, `restore_comment`, `get_history` |
| **Attachments** | `list_attachments`, `add_attachment` (base64, 10 MB), `delete_attachment` |
| **Tags** | `list_tags`, `rename_tag`, `merge_tags` |
| **Bulk** | `create_items`, `reorder_stories` |
| **Project admin** | `list_project_settings`, `create_project_setting`, `update_project_setting`, `delete_project_setting`, `reorder_project_settings`, `set_custom_attribute_values`, `list_members`, `invite_members` |
| **Wiki** | `get_wiki`, `upsert_wiki` |

Multi-type tools take an `entity_type` argument (`story`, `task`, `issue`, `epic`, …) instead of one tool per type. Each tool's description — what an MCP client shows the model — states its exact parameters and side effects. Some worth knowing:

- **Deletes are permanent** (Taiga has no undo). `delete_item` on a story also deletes its tasks; on a sprint it keeps the stories (they return to the backlog); on an epic it keeps the stories. `delete_project_setting` requires `move_to` for statuses, types, priorities, severities and points — items using the deleted value switch to it.
- **Project admin rights** are needed for `rename_tag`, `merge_tags`, the settings writes and `invite_members`; without them the error says so. `invite_members` sends Taiga's invitation emails.
- `reorder_stories` reorders inside one column (backlog, a sprint, a kanban status) and refuses stories from elsewhere — it never moves stories between sprints or statuses.
- `list_attachments` returns download URLs that work without login.

## Running it

### Docker (recommended)

```bash
docker run -i --rm \
  -e TAIGA_API_URL=https://your-taiga-instance.com \
  -e TAIGA_USERNAME=your_username \
  -e TAIGA_PASSWORD=your_password \
  ghcr.io/tetra-2023/pytaiga-mcp:latest
```

MCP client configuration (`.mcp.json`, Claude Code, Cursor, Claude Desktop):

```json
{
  "mcpServers": {
    "taiga": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "--pull", "always",
        "-e", "TAIGA_API_URL", "-e", "TAIGA_USERNAME", "-e", "TAIGA_PASSWORD",
        "ghcr.io/tetra-2023/pytaiga-mcp:latest"
      ]
    }
  }
}
```

Use `-i` without `-t` for stdio. `-e VAR` (no `=value`) forwards the variable from your environment. `--pull always` picks up new releases; without it Docker keeps using the image it already has.

Tags: `:latest` follows `main`, `:stable` is the latest release, `:X.Y.Z` is immutable.

HTTP transports (append the flag, publish the port, bind to all interfaces):

```bash
docker run --rm -p 8000:8000 -e MCP_HOST=0.0.0.0 \
  -e TAIGA_API_URL=... -e TAIGA_USERNAME=... -e TAIGA_PASSWORD=... \
  ghcr.io/tetra-2023/pytaiga-mcp:latest --streamable-http    # or --sse
```

### From source

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/TETRA-2023/pytaiga-mcp.git && cd pytaiga-mcp
./install.sh            # or ./install.sh --dev for tests and linters
cp .env.example .env    # set TAIGA_API_URL, TAIGA_USERNAME, TAIGA_PASSWORD
./run.sh                # stdio; ./run.sh --sse | --streamable-http
```

Equivalent: `uv run mcp-taiga [--sse | --streamable-http]` or `uv run python -m src.server`.
`./inspect.sh` opens the server in the [MCP Inspector](https://github.com/modelcontextprotocol/inspector).

Client configuration without Docker:

```json
{
  "mcpServers": {
    "taiga": {
      "command": "uv",
      "args": ["--directory", "/path/to/pytaiga-mcp", "run", "mcp-taiga"],
      "env": { "TAIGA_API_URL": "https://taiga.example.com" }
    }
  }
}
```

## Configuration

| Variable | Description | Default |
|---|---|---|
| `TAIGA_API_URL` | Taiga base URL: scheme, host and any path prefix (the server adds `/api/v1`; a trailing `/api/v1` is stripped). Use the final `https://` URL: a redirect turns a POST into a GET. | `http://localhost:9000` |
| `TAIGA_USERNAME` / `TAIGA_PASSWORD` | Credentials for auto-authentication at startup. If this login fails, a tool called without a `session_id` reports why. | (none) |
| `TAIGA_TRANSPORT` | `stdio`, `sse` or `streamable-http` (a `--sse` / `--streamable-http` flag wins) | `stdio` |
| `TAIGA_CLOSING_RECEIPTS` | `false` turns off the closing-receipt guard (see below) | `true` |
| `MCP_HOST` | Bind address for the HTTP transports (`0.0.0.0` in Docker) | `127.0.0.1` |
| `MCP_PORT` | Port for the HTTP transports | `8000` |
| `TAIGA_AUTH_MODE` | `app-token`: on `sse` / `streamable-http`, every request needs a personal key; `oauth`: the same plus sign-in through the browser (see below). Ignored on stdio | (off) |
| `TAIGA_MCP_KEY_TTL_SECONDS` | How long a checked key is trusted before Taiga is asked again | `300` |
| `MCP_PUBLIC_URL` | `oauth`: public HTTPS root of this server; MCP is at `/mcp`, `/.well-known/*` at the root | (none) |
| `MCP_OAUTH_SECRET` | `oauth`: server key (32+ chars) that encrypts issued tokens. Changing it signs everyone out | (none) |
| `TAIGA_PUBLIC_URL` | `oauth`: Taiga web address the browser is sent to | `TAIGA_API_URL` |
| `TAIGA_OAUTH_APP_ID` | `oauth`: id of the Taiga external Application whose consent page is shown | `pytaiga-mcp-oauth` |
| `TAIGA_OAUTH_TOKEN_TTL_DAYS` | `oauth`: lifetime of issued access and refresh tokens | `90` |

Values can also come from a `.env` file in the project root.

**Closing receipts.** Moving a user story to a closed status (`Done`, `Cancelled`, …), or a task to a closed status or `Ready for test`, is refused unless a comment posted since the item last left a closed status is a receipt: its first non-empty line starts with `Outcome:`, and it has the other field lines (story: `AC:`, `Scope:`, `Artifacts:`, `Next:`; task: `Decision:`, `Artifact:`, `Next:`). The refusal writes nothing and carries the template. `receipt_override="<reason>"` on `update_story`, `set_story_status`, `update_task` and `set_task_status` closes without a receipt and posts `Closing without receipt: <reason>` with the status, in one write. A task cannot be created in such a status (`create_task`, `break_down_story`).

## Authentication

**Auto-authentication (recommended).** With `TAIGA_USERNAME` and `TAIGA_PASSWORD` set, the server logs in at startup and every tool uses that default session — no `session_id` needed. Taiga access tokens last 24 h; the server refreshes them automatically with the refresh token (8 days on a default Taiga), after which it needs to log in again (restart, or call `login`).

**Explicit sessions.** Call `login(host, username, password)` to get a `session_id`, pass it to tools, and `logout` when done. `session_status` reports whether a session is still valid.

**Personal keys (`TAIGA_AUTH_MODE=app-token`, HTTP only).** For a shared HTTP server where each person acts as themselves. Each person gets a key; Taiga stores only `sha256(key)` as an external-app token (`taiga.external_apps`, on by default) bound to that person, and the server calls Taiga with `Authorization: Application <sha256(key)>`. Everything runs with that person's rights and shows them as the author, including project creation and deletion. There is no shared account: the server refuses to start with `TAIGA_USERNAME`/`TAIGA_PASSWORD` set, `login` and `get_default_session` are refused, and a `session_id` argument is ignored. A request without a valid key gets a plain `401` (no OAuth metadata is published). A revoked key stops working on the next call.

Keys are managed with `scripts/mcp-keys.sh` on the Taiga host. It runs `scripts/mcp_keys.py` through `python manage.py shell` in the `taiga-back` container of the compose project in `TAIGA_COMPOSE_DIR` (default `~/control-plane`):

```bash
scripts/mcp-keys.sh issue <username|email>   # prints the key ONCE; replaces that person's previous key
scripts/mcp-keys.sh list                     # who holds a key; flags inactive users still holding one
scripts/mcp-keys.sh revoke <username|email>
```

One key per person, no expiry. **When someone leaves: `revoke` first, then deactivate the account** — Taiga does not check `is_active` for app tokens (the server also refuses a key whose user `/users/me` reports inactive, but only after its cache entry expires).

Connect a client with the key as a bearer token, e.g. Claude Code:

```bash
claude mcp add --transport http taiga https://taiga.example.com/mcp --header "Authorization: Bearer <key>"
```

**Browser sign-in (`TAIGA_AUTH_MODE=oauth`, HTTP only).** Everything above for personal keys holds, and keys keep working on the same address. In addition the server is an OAuth authorization server for MCP clients (metadata, dynamic client registration, `/authorize`, `/token` with PKCE, `/revoke`), so a client added without a header opens the browser:

```bash
claude mcp add --transport http taiga https://mcp.example.com/mcp
```

The person signs in on Taiga's own page (`/external-apps`, the password never reaches this server) and clicks "Allow"; Taiga sends the browser back to `<MCP_PUBLIC_URL>/oauth/taiga-callback`, and the server trades Taiga's one-time code for that person's external-app token. That token is never handed out as is: access and refresh tokens are it, encrypted with `MCP_OAUTH_SECRET` (no database; client registrations are sealed into their `client_id`). Only sign-ins in progress live in memory, so run one server process. Registration accepts only `http://localhost` / `127.0.0.1` redirects or an app scheme (`cursor://...`). Taiga shows the consent page only the first time; later sign-ins pass straight through.

The Application is created once on the Taiga host: `scripts/mcp-keys.sh oauth-app https://mcp.example.com` (`oauth-app-delete` removes it and signs everyone out). `list` shows both kinds of access (`key`, `browser`); `revoke <user>` removes both. A revoked sign-in stops working on the next call; a client's `/revoke` call does the same for that person.

Credentials are held as secrets and never logged; request bodies, headers and tokens are never logged either.

## Errors

A failing tool returns an MCP error result (`isError: true`) whose text is `Error executing tool <name>: <message>`, for example `… User story #42 not found in project 'my-project'.` or `… API Error 400: subject: This field is required.` Taiga's own validation messages are passed through; a refused admin action says `requires project admin rights`.

## Development

```
src/
  server/            the MCP server package
    app.py           FastMCP instance, lifespan, transports, main(), tool() decorator
    core.py          sessions, per-session cache, error wrapper
    resolve.py       name resolution (project, sprint, status, user, refs), summaries
    tools/           one module per tool group
  taiga_client.py    TaigaHTTP: httpx client for the Taiga REST API (auth + refresh, errors)
  config.py          settings from the environment
tests/
  test_server.py       tools, resolvers, thread offload (mocked HTTP)
  test_taiga_client.py the HTTP client (httpx.MockTransport)
  test_integration.py  against a real Taiga (opt-in)
```

```bash
./install.sh --dev
./run_unit_tests.sh                     # all unit tests
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
# Integration tests against a real Taiga. Write tests need a THROWAWAY project:
TAIGA_API_URL=... TAIGA_USERNAME=... TAIGA_PASSWORD=... \
TAIGA_TEST_PROJECT=<sandbox-slug> uv run pytest tests/test_integration.py -m integration
# The read-only issue-listing test runs on TAIGA_ISSUES_PROJECT (else TAIGA_TEST_PROJECT,
# else the first visible project); pick one that has issues.
```

Pre-commit runs gitleaks, ruff and the unit tests. CI additionally builds the image and smoke-tests it (`initialize` + `tools/list`). See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

- [talhaorak](https://github.com/talhaorak) for the [original pytaiga-mcp](https://github.com/talhaorak/pytaiga-mcp)
- [Taiga](https://www.taiga.io/) and the [Model Context Protocol](https://modelcontextprotocol.io/)
