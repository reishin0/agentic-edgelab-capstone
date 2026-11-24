"""Two local demo services. Knowledge data persists in the project runtime folder."""
import sqlite3
from pathlib import Path
from typing import TypedDict

from mcp.server.fastmcp import FastMCP


class Note(TypedDict):
    id: str
    title: str
    body: str


def math_server(port=8766):
    server = FastMCP("demo-math", host="127.0.0.1", port=port, json_response=True, stateless_http=True)

    @server.tool()
    def add(a: float, b: float) -> float:
        """Add two numbers."""
        return a + b

    @server.tool()
    def multiply(a: float, b: float) -> float:
        """Multiply two numbers."""
        return a * b

    return server


def knowledge_server(data_dir: Path, port=8767):
    data_dir.mkdir(parents=True, exist_ok=True)
    database = data_dir / "notes.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL)")
        connection.execute("INSERT OR IGNORE INTO notes VALUES (?, ?, ?)", ("gateway", "MCP Gateway", "One gateway centralizes discovery, policy and observability for multiple MCP backends."))
        connection.execute("INSERT OR IGNORE INTO notes VALUES (?, ?, ?)", ("scaling", "Scaling boundaries", "Bound queues and isolate failed backends. Measure throughput before making capacity claims."))
    server = FastMCP("demo-knowledge", host="127.0.0.1", port=port, json_response=True, stateless_http=True)

    @server.tool()
    def search_notes(query: str, limit: int = 10) -> list[dict]:
        """Search the local demonstration notes by title or body."""
        if len(query) > 300 or not 1 <= limit <= 50:
            raise ValueError("Query or limit outside supported range")
        with sqlite3.connect(database) as connection:
            rows = connection.execute("SELECT id, title, body FROM notes WHERE instr(lower(title), lower(?)) > 0 OR instr(lower(body), lower(?)) > 0 LIMIT ?", (query, query, limit)).fetchall()
        return [dict(zip(("id", "title", "body"), row)) for row in rows]

    @server.tool()
    def get_note(note_id: str) -> Note:
        """Read one local demonstration note."""
        with sqlite3.connect(database) as connection:
            row = connection.execute("SELECT id, title, body FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise ValueError("Note not found")
        return dict(zip(("id", "title", "body"), row))

    @server.tool()
    def save_note(note_id: str, title: str, body: str) -> dict:
        """Save a local demonstration note. Gateway requires the operator role."""
        if not 1 <= len(note_id) <= 100 or not 1 <= len(title) <= 500 or len(body) > 50000:
            raise ValueError("Note outside supported size limits")
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO notes VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, body=excluded.body", (note_id, title, body))
        return {"saved": note_id}

    return server
