"""Prompt registry: versioned prompt templates addressable by name + label.

Every POST to /prompts/{name} creates a new immutable version. Labels such as
"production" or "staging" are pointers to a version and can be moved.
"""

import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

DB_PATH = os.getenv("PROMPT_DB_PATH", "/data/prompts.db")

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS versions (
                name TEXT NOT NULL,
                version INTEGER NOT NULL,
                template TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (name, version)
            );
            CREATE TABLE IF NOT EXISTS labels (
                name TEXT NOT NULL,
                label TEXT NOT NULL,
                version INTEGER NOT NULL,
                PRIMARY KEY (name, label),
                FOREIGN KEY (name, version) REFERENCES versions (name, version)
            );
            """
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="prompt-registry", lifespan=lifespan)


class PromptIn(BaseModel):
    template: str
    labels: list[str] = []


class LabelIn(BaseModel):
    version: int


def labels_by_version(conn: sqlite3.Connection, name: str) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for row in conn.execute("SELECT label, version FROM labels WHERE name = ?", (name,)):
        out.setdefault(row["version"], []).append(row["label"])
    return out


def to_dict(row: sqlite3.Row, labels: dict[int, list[str]]) -> dict:
    return {
        "name": row["name"],
        "version": row["version"],
        "template": row["template"],
        "created_at": row["created_at"],
        "labels": sorted(labels.get(row["version"], [])),
    }


def set_label(conn: sqlite3.Connection, name: str, label: str, version: int) -> None:
    conn.execute(
        "INSERT INTO labels (name, label, version) VALUES (?, ?, ?) "
        "ON CONFLICT (name, label) DO UPDATE SET version = excluded.version",
        (name, label, version),
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/prompts")
def list_prompts() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT name, MAX(version) AS latest FROM versions GROUP BY name ORDER BY name"
        ).fetchall()
        return [
            {
                "name": r["name"],
                "latest_version": r["latest"],
                "labels": {
                    lbl["label"]: lbl["version"]
                    for lbl in conn.execute(
                        "SELECT label, version FROM labels WHERE name = ?", (r["name"],)
                    )
                },
            }
            for r in rows
        ]


@app.post("/prompts/{name}", status_code=201)
def create_version(name: str, body: PromptIn) -> dict:
    with db() as conn:
        (latest,) = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM versions WHERE name = ?", (name,)
        ).fetchone()
        version = latest + 1
        conn.execute(
            "INSERT INTO versions (name, version, template, created_at) VALUES (?, ?, ?, ?)",
            (name, version, body.template, datetime.now(UTC).isoformat()),
        )
        for label in body.labels:
            set_label(conn, name, label, version)
        row = conn.execute(
            "SELECT * FROM versions WHERE name = ? AND version = ?", (name, version)
        ).fetchone()
        return to_dict(row, labels_by_version(conn, name))


@app.get("/prompts/{name}")
def get_prompt(name: str, label: str | None = None, version: int | None = None) -> dict:
    """Fetch by label, by explicit version, or the latest version if neither is given."""
    with db() as conn:
        if label is not None:
            ref = conn.execute(
                "SELECT version FROM labels WHERE name = ? AND label = ?", (name, label)
            ).fetchone()
            if ref is None:
                raise HTTPException(404, f"prompt {name!r} has no label {label!r}")
            version = ref["version"]
        if version is None:
            row = conn.execute(
                "SELECT * FROM versions WHERE name = ? ORDER BY version DESC LIMIT 1", (name,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM versions WHERE name = ? AND version = ?", (name, version)
            ).fetchone()
        if row is None:
            raise HTTPException(404, f"prompt {name!r} not found")
        return to_dict(row, labels_by_version(conn, name))


@app.get("/prompts/{name}/versions")
def list_versions(name: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM versions WHERE name = ? ORDER BY version", (name,)
        ).fetchall()
        if not rows:
            raise HTTPException(404, f"prompt {name!r} not found")
        labels = labels_by_version(conn, name)
        return [to_dict(r, labels) for r in rows]


@app.put("/prompts/{name}/labels/{label}")
def move_label(name: str, label: str, body: LabelIn) -> dict:
    with db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM versions WHERE name = ? AND version = ?", (name, body.version)
        ).fetchone()
        if exists is None:
            raise HTTPException(404, f"prompt {name!r} has no version {body.version}")
        set_label(conn, name, label, body.version)
        return {"name": name, "label": label, "version": body.version}


@app.delete("/prompts/{name}/labels/{label}", status_code=204)
def delete_label(name: str, label: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM labels WHERE name = ? AND label = ?", (name, label))


@app.delete("/prompts/{name}", status_code=204)
def delete_prompt(name: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM labels WHERE name = ?", (name,))
        deleted = conn.execute("DELETE FROM versions WHERE name = ?", (name,)).rowcount
        if not deleted:
            raise HTTPException(404, f"prompt {name!r} not found")
