# Codex infrastructure MCP stack

A small Docker Compose stack containing:

- a private MCP server with bounded, read-only PostgreSQL queries;
- YAML-allowlisted commands executed on one Linux host over SSH; and
- Codex CLI with persistent authentication, configuration, and conversation state.

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
