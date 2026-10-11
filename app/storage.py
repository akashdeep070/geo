"""Atomic SQLite/PostgreSQL storage: files and results are committed together."""

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import psycopg
from psycopg.types.json import Jsonb


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "geo.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS files "
                "(id TEXT PRIMARY KEY, information TEXT NOT NULL, measurements TEXT NOT NULL)"
            )

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            with connection:
                yield connection

    def save(self, information: dict, features: list[dict]):
        with self.connect() as db:
            db.execute(
                "INSERT INTO files VALUES (?, ?, ?)",
                (
                    information["id"],
                    json.dumps(information, allow_nan=False),
                    json.dumps(features, allow_nan=False),
                ),
            )

    def get(self, file_id: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT information, measurements FROM files WHERE id = ?", (file_id,)
            ).fetchone()
        return (json.loads(row[0]), json.loads(row[1])) if row else None


class PostgresStore:
    """Durable results shared by independent serverless function instances."""

    def __init__(self, database_url: str):
        parsed = urlsplit(database_url)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise ValueError("Configure a valid PostgreSQL DATABASE_URL or POSTGRES_URL.")
        self.database_url = database_url
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        self.connection_options = {
            "connect_timeout": 10,
            "options": "-c statement_timeout=10000",
            "sslmode": "disable" if local else "verify-full",
            "sslrootcert": certifi.where(),
        }
        with self.connect() as db:
            # Serializes first-start DDL across simultaneous cold starts.
            db.execute("SELECT pg_advisory_xact_lock(710448127)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS geo_files "
                "(id TEXT PRIMARY KEY, information JSONB NOT NULL, measurements JSONB NOT NULL)"
            )

    @contextmanager
    def connect(self):
        with psycopg.connect(self.database_url, **self.connection_options) as connection:
            yield connection

    def save(self, information: dict, features: list[dict]):
        def encode(value):
            return json.dumps(value, allow_nan=False)

        with self.connect() as db:
            db.execute(
                "INSERT INTO geo_files (id, information, measurements) VALUES (%s, %s, %s)",
                (
                    information["id"],
                    Jsonb(information, dumps=encode),
                    Jsonb(features, dumps=encode),
                ),
            )

    def get(self, file_id: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT information, measurements FROM geo_files WHERE id = %s", (file_id,)
            ).fetchone()
        return (row[0], row[1]) if row else None
