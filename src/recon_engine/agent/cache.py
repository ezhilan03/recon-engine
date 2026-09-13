"""Content-addressed local caches. Database fingerprints exclude credentials."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

import psycopg


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     allow_nan=False).encode()).hexdigest()


def source_fingerprint():
    # One repeatable-read snapshot covers every table exposed by the MCP tools.
    # Full hashing is deliberately bounded to this portfolio dataset, not a claim
    # of a warehouse-scale change capture mechanism.
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        ledger = conn.execute("SELECT row_to_json(t) FROM internal_ledger t ORDER BY internal_txn_id").fetchall()
        settlements = conn.execute("SELECT row_to_json(t) FROM network_settlement t ORDER BY settlement_line_id").fetchall()
    return digest([ledger, settlements])


def read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".cache-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
