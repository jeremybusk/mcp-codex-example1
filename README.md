# Codex infrastructure MCP stack

A small Docker Compose stack containing:

- a private MCP server with bounded, read-only PostgreSQL queries;
- YAML-allowlisted commands executed on one Linux host over SSH; and
- Codex CLI with persistent authentication, configuration, and conversation state.
- OpenCode CLI as an alternative MCP client with persistent configuration and sessions.
- an HTTPS web console for executing the same allowlisted commands without Codex.

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
