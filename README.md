# Codex infrastructure MCP stack

A small Docker Compose stack containing:

- a private MCP server with bounded, read-only PostgreSQL queries;
- YAML-allowlisted commands executed on one Linux host over SSH; and
- Codex CLI with persistent authentication, configuration, and conversation state.
- OpenCode CLI as an alternative MCP client with persistent configuration and sessions.
- an HTTPS web console for executing the same allowlisted commands without Codex.
- private, compressed Quickwit search over normalized Codex and OpenCode conversations.

The MCP port is only available on the Compose network (it is not published to the host). The network retains outbound access so the MCP container can reach SSH and PostgreSQL targets. Remote commands use fixed argv templates, typed parameters, strict host-key checking, and no user-supplied shell command.

## Quick start

Requirements: Docker Engine with Compose v2, an SSH key accepted by the remote host, and a PostgreSQL login.

```sh
cp .env.example .env
mkdir -p .ssh data/codex workspace
cp /path/to/private-key .ssh/mcp
ssh-keyscan -H server.example.internal > .ssh/known_hosts
chmod 600 .ssh/mcp
chmod 644 .ssh/known_hosts
docker compose build
docker compose up -d
docker compose exec codex codex login --device-auth
docker compose exec codex codex
```

Set the real host and database values in `.env`. `SSH_KEY_FILE` and `SSH_KNOWN_HOSTS_FILE` are host paths and default to files in `./.ssh`. Verify the SSH fingerprint out of band before trusting the `ssh-keyscan` result.

Codex state is bind-mounted at `./data/codex`, including its login and local session history. The working directory is `./workspace`. Both are gitignored and survive container replacement.

## Searchable chat history

`quickwit` and `chat-indexer` start with the normal stack. Quickwit is reachable only on the internal `chat_search` network and stores its index in `./data/quickwit`. The importer scans every 30 seconds, keeps its durable deduplication state in `./data/chat-indexer`, and reads agent data with read-only mounts.

The importer deliberately indexes only user and assistant text:

- Codex `response_item/message` input and output text;
- completed OpenCode `part.type=text` records;
- source, owner, agent, session, project, role, and timestamp metadata.

Reasoning, tool calls, tool output, account/credential tables, and logs are excluded. Common API keys, bearer tokens, password assignments, and PEM private keys are redacted before ingestion. Redaction is defense in depth—not a guarantee that arbitrary secrets pasted into prose will be recognized—so do not paste credentials into chats.

The existing `infra` MCP server exposes two bounded, read-only tools:

- `search_chat_history(query, source?, role?, since?, limit?)`, with a maximum of 50 hits;
- `get_chat_session(session_id, source?, limit?)`, also capped at 50 hits.

Example prompts in either Codex or OpenCode:

```text
Use infra.search_chat_history to find my discussions about Grafana Loki. Search source codex-local and return at most 10 matches.

Use infra.search_chat_history for "docker compose" since 2026-08-01T00:00:00Z. Summarize the relevant decisions and cite each session_id.

Use infra.get_chat_session with session_id <id> and source opencode-local, then summarize that conversation.
```

Check ingestion without printing private results:

```sh
docker compose ps quickwit chat-indexer mcp-server
docker compose logs --tail=50 chat-indexer
```

### Add users or agent types

Each entry in [config/chat-sources.yaml](config/chat-sources.yaml) has a stable source name, parser type, owner, agent label, and container path. To add another Codex or OpenCode user, add an entry and mount that user's history read-only under `/sources` in the `chat-indexer` service. A source name such as `codex-alice` or `opencode-ci-bot` makes agent filtering unambiguous.

`CHAT_HISTORY_OWNER` is fixed on the MCP server and added to every search, preventing its clients from requesting another configured owner through the tool. This is useful routing and accidental-cross-user protection, but a field filter is not a hard multi-tenant security boundary. For mutually untrusted users, run separate MCP/indexer/Quickwit stacks (or separate indexes plus independently authenticated gateways); never give those users direct Quickwit network access. Quickwit is append-oriented, so changing the parser, redaction policy, or indexed document shape requires choosing a new `CHAT_INDEX_ID` and rebuilding that index.

The index uses Quickwit's level-8 doc-store compression and persists indefinitely by default. To impose a retention window, add a `retention` block to `config/chat-history-index.yaml` before first index creation, then use a new index ID. Removing `./data/quickwit` deletes the searchable copy but not the original Codex/OpenCode histories; removing `./data/chat-indexer` makes the importer forget its deduplication state and must only be done together with the corresponding Quickwit index reset.

## OpenCode client

OpenCode is available as an alternative to Codex and connects to the same `infra` MCP server:

```sh
docker compose exec opencode opencode
```

Use `/connect` inside OpenCode to authenticate an LLM provider, then ask it to use the `infra` MCP tools. Verify connectivity with:

```sh
docker compose exec opencode opencode mcp list
```

Everything OpenCode owns is persisted beneath `./data/opencode`: `opencode.json`, provider credentials, MCP credentials, sessions, logs, and cache. On first startup, [config/opencode.json](config/opencode.json) is copied to `./data/opencode/opencode.json`. That persistent copy is authoritative afterward, so edit it directly for later configuration changes.

## Grafana Cloud MCP

The optional `grafana-mcp` service uses a digest-pinned official Grafana MCP image in Streamable HTTP, read-only mode. It is isolated on a client-only Compose network shared with Codex and OpenCode, and is configured in both clients as `grafana`.

Create a dedicated Grafana service account with only the datasource/dashboard permissions the agents need. Configure the account metadata and token in the gitignored `.env`:

```env
GRAFANA_URL=https://curiousmustard3132.grafana.net/
GF_SERVICE_ACCOUNT_NAME=grafana-mcp
GF_SERVICE_ACCOUNT_TOKEN=glsa_your_service_account_token
GRAFANA_MCP_SERVER_TOKEN=generate-a-separate-value-with-openssl-rand-hex-32
```

Start the optional profile and recreate the clients so they reconnect:

```sh
docker compose --profile grafana up -d grafana-mcp
docker compose restart codex opencode
docker compose exec codex codex mcp list
docker compose exec opencode opencode mcp list
```

The endpoint is `http://grafana-mcp:8000/mcp` and is not published to the host. Compose maps `GF_SERVICE_ACCOUNT_TOKEN` to the official container's `GRAFANA_SERVICE_ACCOUNT_TOKEN`; the account name is retained for operator identification but token authentication does not require a username. A separate `GRAFANA_MCP_SERVER_TOKEN` authenticates Codex and OpenCode to the MCP endpoint; generate one with `openssl rand -hex 32`. `--disable-write` prevents MCP write operations even if the Grafana service account has broader permissions. Loki responses are limited to 200 lines. Ask an agent, for example: `Use the grafana MCP server to query Loki for {job="uvoo.io"} over the last hour.`

## Web command console

The browser UI talks to the MCP server as an MCP client; it does not execute commands itself. Caddy is the only published service and provides HTTPS plus hashed basic authentication.

Before first startup, generate credentials and put them in `.env`:

```sh
docker run --rm caddy:2.10-alpine caddy hash-password --plaintext 'choose-a-long-password'
openssl rand -hex 32
```

Save the first output as `WEB_PASSWORD_HASH` and the second as `WEB_SESSION_SECRET`. Compose defaults to `WEB_USERNAME=admin` and binds to localhost only:

```text
https://localhost:8443
```

Caddy uses its private CA for local HTTPS. Trust `./data/caddy/data/caddy/pki/authorities/local/root.crt` on client devices, or accept the browser warning for testing. To allow other LAN devices, set both `WEB_BIND_ADDRESS` and `WEB_HOST` to the Docker host's specific LAN IP (or set `WEB_HOST` to its DNS name)—not `0.0.0.0` unless every attached network is intended. A VPN address is preferable for remote access.

The UI generates typed controls from `commands.yaml`, displays raw stdout/stderr, requires exact-name confirmation for commands whose `confirmation` is true, uses strict CSRF/session cookies, and appends audit metadata to `./data/web/audit.jsonl`. Output itself is deliberately not written to the audit log.

Policy metadata defaults conservatively to `risk: write` and `confirmation: true`. Mark reviewed read-only commands explicitly:

```yaml
  uptime:
    enabled: true
    risk: read
    confirmation: false
    description: Show server uptime and load.
    argv: ["/usr/bin/uptime"]
```

## Command policy

Edit `config/commands.yaml`. Changes are reloaded for every tool call, so no rebuild or restart is needed. A command must:

- have `enabled: true`;
- begin with an absolute remote executable path;
- define its complete argument vector in `argv`; and
- validate every substituted parameter as `enum`, `integer`, `ip`, or a constrained `string`.

Prefer `enum` parameters. Add `sudo -n` explicitly where required. Do not add shells (`sh`, `bash`), interpreters, editors, command runners, or broadly expressive programs to the allowlist; they turn a narrow command into arbitrary execution.

Example:

```yaml
commands:
  restart_nginx:
    enabled: false
    description: Restart nginx.
    argv: ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "nginx"]
```

Set `enabled: true` only after reviewing the impact. The best defense is also a narrowly scoped remote sudoers rule instead of unrestricted passwordless sudo. This MCP allowlist limits agents, but it is not a security boundary if the SSH account can bypass it by other means.

## Database safety

Use a dedicated PostgreSQL role granted only `CONNECT`, `USAGE` on required schemas, and `SELECT` on required tables. The server additionally starts a read-only transaction and applies row, statement-time, lock-time, and query-size limits. SQL is still intentionally flexible enough for CTEs and database-specific read functions, so database privileges remain the authoritative boundary.

## Operations

```sh
docker compose logs -f mcp-server
docker compose restart mcp-server
docker compose down
```

`docker compose down` preserves the bind-mounted files. Do not commit `.env`, `data/`, private keys, or Codex state. To rotate Codex authentication, run `docker compose exec codex codex logout`, then log in again.
