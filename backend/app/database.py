"""Backend-only PostgreSQL connections; credentials never belong in responses."""

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row


def connect_db():
    """Return a new connection for each operation/request.

    Use as a context manager to commit on success and roll back on failure.
    Separate parameters allow passwords containing special characters.
    """
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    required = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
    if any(not os.getenv(key) for key in required):
        raise RuntimeError("Missing database configuration in backend/.env")
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        sslmode=os.getenv("DB_SSLMODE", "require"),
        connect_timeout=10,
        row_factory=dict_row,
    )
