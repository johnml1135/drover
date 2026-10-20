"""Connection, transactions and migrations. The Windows hub is the only opener (ADR 0001)."""
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

from .. import config, placement
from .schema import MIGRATIONS



class HubRequired(RuntimeError):
    pass


def default_path(env=None):
    env = os.environ if env is None else env
    if env.get("DROVER_LEDGER"):
        return Path(env["DROVER_LEDGER"])
    if os.name == "nt":
        return config.state_path("ledger.db")
    raise HubRequired("the ledger lives on Windows; from WSL use the Drover Hub API (or set DROVER_LEDGER for a test db)")


def connect(path, migrate_db=True):
    path = Path(path)
    if placement.is_drvfs(path):
        raise HubRequired("WAL does not work on DrvFs (/mnt/*); the ledger must live on a local filesystem")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        conn.close()
        raise RuntimeError(f"could not enable WAL on {path} (journal_mode={mode})")
    conn.execute("PRAGMA synchronous=NORMAL")
    if migrate_db:
        migrate(conn)
    return conn


@contextmanager
def tx(conn):
    """One write transaction (BEGIN IMMEDIATE); joins the caller's transaction when one is open."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    try:
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def schema_version(conn):
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone()
    if not has:
        return 0
    return conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]


def migrate(conn, migrations=None):
    """Apply pending migrations in order, each in one transaction. Safe to call repeatedly."""
    migrations = MIGRATIONS if migrations is None else migrations
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied TEXT NOT NULL)")
    for version, sql in sorted(migrations):
        if schema_version(conn) >= version:
            continue
        # executescript commits first, so the transaction lives inside the script itself
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + sql + "\nINSERT INTO schema_version(version, applied) "
                f"VALUES ({int(version)}, strftime('%Y-%m-%dT%H:%M:%SZ','now'));\nCOMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return schema_version(conn)


def main_error(msg):
    print(f"drover.ledger: {msg}", file=sys.stderr)
    return 2
