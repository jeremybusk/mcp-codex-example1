import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.middleware.sessions import SessionMiddleware


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("infra-web")
MCP_URL = os.getenv("MCP_URL", "http://mcp-server:8000/mcp")
AUDIT_PATH = Path(os.getenv("AUDIT_PATH", "/data/audit.jsonl"))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["WEB_SESSION_SECRET"],
    https_only=True,
    same_site="strict",
    max_age=3600,
)
templates = Jinja2Templates(directory="/app/templates")
app.mount("/static", StaticFiles(directory="/app/static"), name="static")


def unwrap(result: Any) -> Any:
    data = result.structuredContent
    if data is not None:
        return data.get("result", data) if isinstance(data, dict) else data
    for block in result.content:
        if getattr(block, "type", "") == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return block.text
    return None


async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    async with streamable_http_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments or {})
            if result.isError:
                message = "\n".join(getattr(item, "text", str(item)) for item in result.content)
                raise RuntimeError(message)
            return unwrap(result)


def csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def audit(entry: dict[str, Any]) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    error = None
    try:
        commands = await call_tool("list_commands")
    except Exception as exc:
        log.exception("unable to list commands")
        commands, error = [], str(exc)
    return templates.TemplateResponse(request, "index.html", {
        "commands": commands or [], "csrf": csrf(request), "error": error,
        "user": request.headers.get("x-authenticated-user", "admin"),
    })


@app.post("/run", response_class=HTMLResponse)
async def run(
    request: Request,
    command: str = Form(...),
    csrf_token: str = Form(...),
    confirmed: str | None = Form(None),
):
    if not secrets.compare_digest(csrf(request), csrf_token):
        raise HTTPException(403, "invalid CSRF token")
    commands = await call_tool("list_commands") or []
    policy = next((item for item in commands if item["name"] == command), None)
    if not policy:
        raise HTTPException(403, "command is not enabled")
    if policy.get("confirmation", True) and confirmed != command:
        raise HTTPException(400, f"confirmation must exactly match {command}")
    form = await request.form()
    supplied = {
        name: form[f"param_{name}"]
        for name in policy.get("parameters", {})
        if f"param_{name}" in form and form[f"param_{name}"] != ""
    }

    started = time.monotonic()
    user = request.headers.get("x-authenticated-user", "admin")
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user": user,
        "command": command,
        "parameters": supplied,
    }
    try:
        result = await call_tool("run_remote_command", {"command": command, "parameters": supplied})
        entry.update(
            duration_ms=round((time.monotonic() - started) * 1000),
            exit_code=result.get("exit_code"),
            truncated=result.get("truncated", False),
        )
        audit(entry)
        return templates.TemplateResponse(request, "result.html", {
            "command": command, "parameters": supplied, "result": result,
            "csrf": csrf(request), "user": user,
        })
    except Exception as exc:
        entry.update(duration_ms=round((time.monotonic() - started) * 1000), error=str(exc))
        audit(entry)
        raise HTTPException(502, str(exc)) from exc


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    if not secrets.compare_digest(csrf(request), csrf_token):
        raise HTTPException(403, "invalid CSRF token")
    request.session.clear()
    return RedirectResponse("/", status_code=303)
