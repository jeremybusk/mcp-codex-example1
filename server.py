import ipaddress
import json
import logging
import os
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import yaml
from mcp.server.fastmcp import FastMCP
from psycopg.rows import dict_row


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("infra-mcp")

CONFIG_PATH = Path(os.getenv("MCP_CONFIG", "/app/config/commands.yaml"))
DB_DSN = os.getenv("DATABASE_URL", "")
SSH_HOST = os.getenv("SSH_HOST", "")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))
SSH_USER = os.getenv("SSH_USER", "ubuntu")
SSH_KEY = os.getenv("SSH_KEY", "/run/secrets/ssh_key")
SSH_KNOWN_HOSTS = os.getenv("SSH_KNOWN_HOSTS", "/app/ssh/known_hosts")
QUICKWIT_URL = os.getenv("QUICKWIT_URL", "").rstrip("/")
CHAT_INDEX_ID = os.getenv("CHAT_INDEX_ID", "chat-history-v1")
CHAT_HISTORY_OWNER = os.getenv("CHAT_HISTORY_OWNER", "local")

mcp = FastMCP(
    "infra-mcp",
    host="0.0.0.0",
    port=int(os.getenv("MCP_PORT", "8000")),
)


def load_config() -> dict[str, Any]:
    """Reload on every call so allowlist edits need no container rebuild."""
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config.get("commands", {}), dict):
        raise ValueError("commands must be a YAML mapping")
    return config


def validate_value(name: str, value: Any, rule: dict[str, Any]) -> str:
    value = str(value)
    kind = rule.get("type", "string")
    if kind == "ip":
        value = str(ipaddress.ip_address(value))
    elif kind == "integer":
        number = int(value)
        if number < int(rule.get("min", number)) or number > int(rule.get("max", number)):
            raise ValueError(f"{name} is outside the allowed range")
        value = str(number)
    elif kind == "enum":
        allowed = [str(item) for item in rule.get("values", [])]
        if value not in allowed:
            raise ValueError(f"{name} must be one of: {', '.join(allowed)}")
    elif kind == "string":
        if len(value) > int(rule.get("max_length", 128)):
            raise ValueError(f"{name} is too long")
        pattern = rule.get("pattern", r"^[A-Za-z0-9._:/@+-]+$")
        if not re.fullmatch(pattern, value):
            raise ValueError(f"{name} contains disallowed characters")
    else:
        raise ValueError(f"unsupported parameter type for {name}: {kind}")
    return value


def build_argv(command_name: str, supplied: dict[str, Any]) -> tuple[list[str], int]:
    config = load_config()
    command = config.get("commands", {}).get(command_name)
    if not command or not command.get("enabled", False):
        raise ValueError(f"command is not enabled: {command_name}")

    rules = command.get("parameters", {})
    unknown = set(supplied) - set(rules)
    if unknown:
        raise ValueError(f"unknown parameters: {', '.join(sorted(unknown))}")

    values: dict[str, str] = {}
    for name, rule in rules.items():
        if name not in supplied:
            if "default" not in rule:
                raise ValueError(f"missing parameter: {name}")
            raw = rule["default"]
        else:
            raw = supplied[name]
        values[name] = validate_value(name, raw, rule)

    argv = []
    for item in command.get("argv", []):
        try:
            argv.append(str(item).format_map(values))
        except KeyError as exc:
            raise ValueError(f"argv references undefined parameter: {exc.args[0]}") from exc
    if not argv or not argv[0].startswith("/"):
        raise ValueError("each command must use an absolute executable path")
    timeout = min(int(command.get("timeout", 20)), 120)
    return argv, timeout


def ssh_argv(remote_argv: list[str]) -> list[str]:
    if not SSH_HOST:
        raise ValueError("SSH_HOST is not configured")
    # OpenSSH sends one command string. POSIX quoting preserves the validated argv
    # as distinct arguments and prevents shell metacharacters from taking effect.
    remote_command = " ".join(shlex.quote(item) for item in remote_argv)
    return [
        "/usr/bin/ssh", "-T", "-i", SSH_KEY, "-p", str(SSH_PORT),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
        "-o", "ConnectTimeout=10", f"{SSH_USER}@{SSH_HOST}", "--", remote_command,
    ]


def _query_term(value: str) -> str:
    """Quote an exact metadata term for Quickwit's query parser."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _chat_search(query: str, source: str = "", role: str = "", session_id: str = "",
                 since: str = "", limit: int = 20) -> dict[str, Any]:
    if not QUICKWIT_URL:
        raise ValueError("chat history search is not configured")
    if not query or len(query) > 1000:
        raise ValueError("query must contain 1 to 1000 characters")
    if not 1 <= int(limit) <= 50:
        raise ValueError("limit must be between 1 and 50")
    for name, value in (("source", source), ("session_id", session_id)):
        if value and (len(value) > 160 or not re.fullmatch(r"[A-Za-z0-9._:/@+-]+", value)):
            raise ValueError(f"invalid {name}")
    if role and role not in {"user", "assistant"}:
        raise ValueError("role must be user or assistant")

    # Quote user text so it cannot inject field filters or expensive query operators.
    text_clause = "text:*" if query == "*" else f"text:{_query_term(query)}"
    clauses = [f"owner:{_query_term(CHAT_HISTORY_OWNER)}", text_clause]
    if source: clauses.append(f"source:{_query_term(source)}")
    if role: clauses.append(f"role:{_query_term(role)}")
    if session_id: clauses.append(f"session_id:{_query_term(session_id)}")
    payload: dict[str, Any] = {
        "query": " AND ".join(clauses), "max_hits": int(limit),
        "sort_by": "-timestamp", "snippet_fields": "text",
    }
    if since:
        try:
            payload["start_timestamp"] = int(datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp())
        except ValueError as exc:
            raise ValueError("since must be an ISO-8601 timestamp") from exc
    request = urllib.request.Request(
        f"{QUICKWIT_URL}/api/v1/{CHAT_INDEX_ID}/search",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", "replace")
        raise RuntimeError(f"chat search failed ({exc.code}): {detail}") from exc
    hits = result.get("hits", [])
    return {"count": len(hits), "elapsed_micros": result.get("elapsed_time_micros"), "hits": hits}


@mcp.tool()
def list_commands() -> list[dict[str, Any]]:
    """List enabled remote commands and their accepted parameters."""
    result = []
    for name, item in load_config().get("commands", {}).items():
        if item.get("enabled", False):
            result.append({
                "name": name,
                "description": item.get("description", ""),
                "parameters": item.get("parameters", {}),
                "risk": item.get("risk", "write"),
                "confirmation": item.get("confirmation", True),
            })
    return result


@mcp.tool()
def run_remote_command(command: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one enabled YAML-defined command on the configured Linux host."""
    argv, timeout = build_argv(command, parameters or {})
    log.info("running allowed command %s", command)
    try:
        result = subprocess.run(
            ssh_argv(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out after {timeout}s") from exc
    output_limit = int(load_config().get("settings", {}).get("max_output_bytes", 65536))
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout[:output_limit],
        "stderr": result.stderr[:output_limit],
        "truncated": len(result.stdout) > output_limit or len(result.stderr) > output_limit,
    }


@mcp.tool()
def query_postgres(sql: str) -> dict[str, Any]:
    """Run one read-only PostgreSQL query and return bounded JSON rows."""
    if not DB_DSN:
        raise ValueError("DATABASE_URL is not configured")
    settings = load_config().get("database", {})
    if not settings.get("enabled", True):
        raise ValueError("database tool is disabled")
    if len(sql) > int(settings.get("max_query_bytes", 10000)):
        raise ValueError("query is too large")
    max_rows = min(int(settings.get("max_rows", 200)), 1000)
    timeout_ms = min(int(settings.get("statement_timeout_ms", 5000)), 30000)
    with psycopg.connect(DB_DSN, autocommit=False, row_factory=dict_row) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", (f"{timeout_ms}ms",))
            cursor.execute("SELECT set_config('lock_timeout', '2s', true)")
            cursor.execute(sql)
            if cursor.description is None:
                raise ValueError("query did not return rows")
            rows = cursor.fetchmany(max_rows + 1)
            if len(rows) > max_rows:
                raise ValueError(f"query exceeds the {max_rows}-row limit")
            return {"row_count": len(rows), "rows": json.loads(json.dumps(rows, default=str))}


@mcp.tool()
def search_chat_history(query: str, source: str = "", role: str = "", since: str = "",
                        limit: int = 20) -> dict[str, Any]:
    """Search this MCP identity's redacted Codex/OpenCode chat text. Source examples: codex-local, opencode-local."""
    return _chat_search(query=query, source=source, role=role, since=since, limit=limit)


@mcp.tool()
def get_chat_session(session_id: str, source: str = "", limit: int = 50) -> dict[str, Any]:
    """Return recent indexed user/assistant messages for one exact chat session ID."""
    return _chat_search(query="*", source=source, session_id=session_id, limit=limit)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
