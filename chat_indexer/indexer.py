import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import yaml

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("chat-indexer")
QUICKWIT_URL = os.getenv("QUICKWIT_URL", "http://quickwit:7280").rstrip("/")
INDEX_ID = os.getenv("CHAT_INDEX_ID", "chat-history-v1")
SOURCES_PATH = Path(os.getenv("CHAT_SOURCES_CONFIG", "/config/chat-sources.yaml"))
INDEX_CONFIG_PATH = Path(os.getenv("CHAT_INDEX_CONFIG", "/config/chat-history-index.yaml"))
STATE_PATH = Path(os.getenv("CHAT_INDEX_STATE", "/state/state.sqlite"))
SCAN_SECONDS = max(5, int(os.getenv("CHAT_SCAN_SECONDS", "30")))
MAX_TEXT = min(262144, int(os.getenv("CHAT_MAX_TEXT_BYTES", "65536")))

SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?token|secret|password)\s*[=:]\s*)[^\s,;\"']+"),
    re.compile(r"\b(?:sk|glsa|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


def redact(text: str) -> str:
    value = text
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", value)
    return value.encode("utf-8")[:MAX_TEXT].decode("utf-8", "ignore")


def iso_timestamp(value) -> str:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    try:
        number = float(value)
        if number > 10_000_000_000: number /= 1000
        return datetime.fromtimestamp(number, timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def document(source_name, cfg, session_id, message_id, timestamp, role, text, ordinal, project=""):
    text = redact(text).strip()
    if not text: return None
    return {
        "timestamp": iso_timestamp(timestamp), "owner": str(cfg.get("owner", "local")),
        "source": source_name, "agent": str(cfg.get("agent", cfg["type"])),
        "session_id": str(session_id), "message_id": str(message_id),
        "project": str(project or ""), "role": role, "ordinal": max(0, int(ordinal)), "text": text,
    }


def codex_documents(source_name: str, cfg: dict) -> Iterator[dict]:
    root = Path(cfg["path"])
    for path in sorted((root / "sessions").glob("**/rollout-*.jsonl")):
        session_id, project, ordinal = path.stem, "", 0
        try: lines = path.open(encoding="utf-8")
        except OSError: continue
        with lines:
            for line in lines:
                try: record=json.loads(line); payload=record.get("payload", {})
                except (json.JSONDecodeError, AttributeError): continue
                if record.get("type") == "session_meta":
                    session_id=payload.get("id", session_id); project=payload.get("cwd", project); continue
                if record.get("type") != "response_item" or payload.get("type") != "message": continue
                role=payload.get("role")
                if role not in {"user", "assistant"}: continue
                for part in payload.get("content", []):
                    if part.get("type") not in {"input_text", "output_text"} or not isinstance(part.get("text"), str): continue
                    ordinal += 1
                    item=document(source_name, cfg, session_id, f"{path.name}:{ordinal}", record.get("timestamp"), role, part["text"], ordinal, project)
                    if item: yield item


def opencode_documents(source_name: str, cfg: dict) -> Iterator[dict]:
    path = Path(cfg["path"])
    if not path.exists(): return
    conn = None
    try:
        conn=sqlite3.connect(f"file:{path}?mode=ro", uri=True); conn.row_factory=sqlite3.Row
        rows=conn.execute("SELECT p.id part_id,p.session_id,p.time_created part_time,p.data part_data,m.data message_data,s.title,s.directory FROM part p JOIN message m ON m.id=p.message_id JOIN session s ON s.id=p.session_id ORDER BY p.time_created,p.id")
        for ordinal, row in enumerate(rows, 1):
            try: part=json.loads(row["part_data"]); message=json.loads(row["message_data"])
            except (json.JSONDecodeError, TypeError): continue
            role=message.get("role")
            if part.get("type") != "text" or role not in {"user", "assistant"} or not isinstance(part.get("text"), str): continue
            if role == "assistant" and not (message.get("time") or {}).get("completed"): continue
            item=document(source_name, cfg, row["session_id"], row["part_id"], row["part_time"], role, part["text"], ordinal, row["directory"] or row["title"] or "")
            if item: yield item
    except sqlite3.Error as exc: log.warning("cannot read OpenCode database %s: %s", path, exc)
    finally:
        if conn: conn.close()


def request(path: str, data: bytes | None = None, content_type="application/json"):
    req=urllib.request.Request(QUICKWIT_URL+path, data=data, headers={"Content-Type":content_type}, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as response: return response.read()


def ensure_index():
    try: request(f"/api/v1/indexes/{INDEX_ID}"); return
    except urllib.error.HTTPError as exc:
        if exc.code != 404: raise
    request("/api/v1/indexes", INDEX_CONFIG_PATH.read_bytes(), "application/yaml")
    log.info("created Quickwit index %s", INDEX_ID)


def state_connection():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True); conn=sqlite3.connect(STATE_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS indexed (fingerprint TEXT PRIMARY KEY, indexed_at INTEGER NOT NULL)")
    return conn


def ingest(conn, docs, fingerprints):
    body=b"\n".join(json.dumps(doc, ensure_ascii=False).encode() for doc in docs)+b"\n"
    request(f"/api/v1/{INDEX_ID}/ingest?commit=auto", body, "application/x-ndjson")
    now=int(time.time()); conn.executemany("INSERT OR IGNORE INTO indexed VALUES (?,?)", ((x,now) for x in fingerprints)); conn.commit()
    log.info("indexed %d chat messages", len(docs))


def scan_once(conn):
    config=yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}; pending=[]; fingerprints=[]
    for source_name,cfg in config.get("sources", {}).items():
        iterator=codex_documents(source_name,cfg) if cfg.get("type")=="codex" else opencode_documents(source_name,cfg) if cfg.get("type")=="opencode" else ()
        for doc in iterator:
            encoded=json.dumps(doc,sort_keys=True,ensure_ascii=False).encode(); fingerprint=hashlib.sha256(encoded).hexdigest()
            if conn.execute("SELECT 1 FROM indexed WHERE fingerprint=?",(fingerprint,)).fetchone(): continue
            pending.append(doc); fingerprints.append(fingerprint)
            if len(pending)>=250: ingest(conn,pending,fingerprints); pending=[]; fingerprints=[]
    if pending: ingest(conn,pending,fingerprints)


def main():
    conn=state_connection()
    while True:
        try: ensure_index(); scan_once(conn)
        except Exception: log.exception("indexing pass failed")
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__": main()
