import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE IF NOT EXISTS chat_sessions (id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create(self, parsed: dict, *, target: str, mode: str, title: str, reporter: str = "", project: str = "", environment: str = "", default_hints: dict | None = None) -> dict:
        batch = {**parsed, "id": str(uuid4()), "mode": mode, "target": target, "title": title,
                 "revision": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                 "defaults": {"reporter": reporter, "version": "", "device": "", "environment": environment, "project": project}, "default_hints": default_hints or {}}
        with self.connection() as conn:
            conn.execute("INSERT INTO batches VALUES (?, ?)", (batch["id"], json.dumps(batch, ensure_ascii=False)))
        return batch

    def get(self, batch_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT body FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise KeyError(batch_id)
        return json.loads(row[0])

    def recent(self, limit: int = 20) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT body FROM batches ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def mutate(self, batch_id: str, callback) -> dict:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT body FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise KeyError(batch_id)
            batch = json.loads(row[0])
            callback(batch)
            batch["revision"] += 1
            conn.execute("UPDATE batches SET body=? WHERE id=?", (json.dumps(batch, ensure_ascii=False), batch_id))
            return batch

    def recover(self):
        for batch in self.recent(limit=100000):
            if not any(item["status"] == "submitting" for item in batch["items"]):
                continue
            def mark_unknown(value):
                for item in value["items"]:
                    if item["status"] == "submitting":
                        item.update(status="unknown", error="上次提交中断，结果待确认。重试会沿用原提交内容及操作编号。")
            self.mutate(batch["id"], mark_unknown)

    def reserve_event(self, event_id: str) -> bool:
        with self.connection() as conn:
            return conn.execute("INSERT OR IGNORE INTO events VALUES (?)", (event_id,)).rowcount == 1

    def chat_session(self, chat_id: str, user_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT body FROM chat_sessions WHERE id=?", (chat_id + ':' + user_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_chat_session(self, chat_id: str, user_id: str, body: dict):
        with self.connection() as conn:
            conn.execute("INSERT INTO chat_sessions VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (chat_id + ':' + user_id, json.dumps(body, ensure_ascii=False)))
