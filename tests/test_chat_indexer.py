import importlib.util
import json
import sqlite3
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("chat_indexer", Path("chat_indexer/indexer.py"))
indexer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(indexer)


def test_redacts_common_secrets():
    text = "Authorization: Bearer abc.def api_key=supersecret glsa_abcdefghijklmnop"
    output = indexer.redact(text)
    assert "abc.def" not in output
    assert "supersecret" not in output
    assert "glsa_" not in output
    assert output.count("[REDACTED]") == 3


def test_codex_parser_excludes_reasoning_and_tools(tmp_path):
    path = tmp_path / "sessions" / "2026" / "01" / "01"
    path.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"id": "s1", "cwd": "/workspace"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:00Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}},
        {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "private thought"}]}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "tool secret"}},
    ]
    (path / "rollout-test.jsonl").write_text("\n".join(json.dumps(x) for x in records))
    docs = list(indexer.codex_documents("codex-a", {"path": str(tmp_path), "type": "codex", "owner": "alice"}))
    assert len(docs) == 1
    assert docs[0]["text"] == "hello"
    assert docs[0]["owner"] == "alice"


def test_opencode_parser_only_completed_text(tmp_path):
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
      CREATE TABLE session(id TEXT, title TEXT, directory TEXT);
      CREATE TABLE message(id TEXT, session_id TEXT, data TEXT);
      CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
    """)
    conn.execute("INSERT INTO session VALUES ('s1','title','/workspace')")
    conn.execute("INSERT INTO message VALUES ('m1','s1',?)", (json.dumps({"role":"assistant","time":{"completed":1}}),))
    conn.execute("INSERT INTO part VALUES ('p1','m1','s1',1,?)", (json.dumps({"type":"text","text":"answer"}),))
    conn.execute("INSERT INTO part VALUES ('p2','m1','s1',2,?)", (json.dumps({"type":"reasoning","text":"thought"}),))
    conn.commit(); conn.close()
    docs = list(indexer.opencode_documents("oc-a", {"path": str(db), "type": "opencode"}))
    assert [doc["text"] for doc in docs] == ["answer"]
