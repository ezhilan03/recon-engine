"""
MCP server for the reconciliation engine, built on the mcp[cli]==2.0.0
stable SDK (2026-07-28 spec).

Three tools, each a thin wrapper over a SQL query against the same
Postgres tables the deterministic matcher reads. The point of exposing
these as MCP tools rather than baking them into agent code is that any
MCP-speaking host (not just this LangGraph app) could reuse them --
e.g. a future ops dashboard or a different investigation agent.

Run standalone (for the MCP Inspector):
    uv run mcp dev src/recon_engine/mcp_server/server.py

The LangGraph investigator node will connect to this in-process rather
than over a socket -- the v2 SDK supports passing the MCPServer instance
directly to Client(), same pattern as FastAPI's TestClient. No subprocess.
"""

import os
from datetime import date, timedelta
from itertools import combinations

import psycopg
from dotenv import load_dotenv
from mcp.server import MCPServer

load_dotenv()

mcp = MCPServer("recon-engine-investigator")

BATCH_SEARCH_WINDOW_DAYS = 25
BATCH_SEARCH_AMOUNT_TOL = 0.01
BATCH_SEARCH_MAX_SIZE = 4


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"])


@mcp.tool()
def lookup_transaction_history(account_last4: str, around_date: str, window_days: int = 30) -> list[dict]:
    """Look up an account's internal transaction history within a window
    of a given date. Use this to check whether a proposed match or batch
    grouping is plausible given the account's normal transaction pattern
    -- e.g. does this account transact multiple times a day (raising
    collision risk for amount-only matching), or once every few days?

    Args:
        account_last4: last 4 digits of the account to look up
        around_date: ISO date (YYYY-MM-DD) to center the window on
        window_days: how many days before/after around_date to include
    """
    center = date.fromisoformat(around_date)
    start = (center - timedelta(days=window_days)).isoformat()
    end = (center + timedelta(days=window_days)).isoformat()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT internal_txn_id, transaction_date, amount, merchant_descriptor, nacha_return_code
            FROM internal_ledger
            WHERE account_last4 = %s AND transaction_date BETWEEN %s AND %s
            ORDER BY transaction_date
        """, (account_last4, start, end))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@mcp.tool()
def search_settlement_lines(
    date_from: str, date_to: str, min_amount: float | None = None, max_amount: float | None = None,
) -> list[dict]:
    """Search the settlement file by date range and optional amount range,
    without filtering by account -- useful when a transaction's own
    account_last4 doesn't appear anywhere nearby (the signature of a
    batched settlement, where the line was tagged with a different
    member's account).

    Args:
        date_from: ISO date, inclusive start of settlement_date range
        date_to: ISO date, inclusive end of settlement_date range
        min_amount: optional lower bound on gross_amount
        max_amount: optional upper bound on gross_amount
    """
    query = """
        SELECT settlement_line_id, settlement_date, gross_amount, account_last4,
               descriptor, batch_id, return_code
        FROM network_settlement
        WHERE settlement_date BETWEEN %s AND %s
    """
    params: list = [date_from, date_to]
    if min_amount is not None:
        query += " AND gross_amount >= %s"
        params.append(min_amount)
    if max_amount is not None:
        query += " AND gross_amount <= %s"
        params.append(max_amount)
    query += " ORDER BY settlement_date"

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@mcp.tool()
def get_batch_context(settlement_line_id: str) -> dict:
    """Given a settlement line, return its full detail plus every other
    settlement line sharing the same batch_id. Real batch members should
    show batch_id continuity; if a proposed batch match has no such
    continuity, that's evidence the match is a coincidental amount
    collision rather than a genuine batch.

    Args:
        settlement_line_id: the settlement line to inspect
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT settlement_line_id, settlement_date, gross_amount, account_last4,
                   descriptor, batch_id, return_code
            FROM network_settlement WHERE settlement_line_id = %s
        """, (settlement_line_id,))
        row = cur.fetchone()
        if row is None:
            return {"error": f"no settlement line found with id {settlement_line_id}"}
        cols = [d[0] for d in cur.description]
        target = dict(zip(cols, row))

        cur.execute("""
            SELECT settlement_line_id, settlement_date, gross_amount, account_last4, descriptor
            FROM network_settlement WHERE batch_id = %s AND settlement_line_id != %s
        """, (target["batch_id"], settlement_line_id))
        siblings = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

    return {"target": target, "same_batch_id_siblings": siblings}


@mcp.tool()
def search_batch_candidates(internal_txn_id: str, window_days: int = 25) -> dict:
    """Search for a batch grouping this transaction might belong to: looks
    for OTHER internal transactions (within a date window) whose amounts,
    combined with this one, sum to match a settlement line's gross_amount.
    Use this when get_batch_context and lookup_transaction_history haven't
    found a direct match.

    IMPORTANT LIMITATION, stated plainly: a numeric sum match is the ONLY
    signal this tool can produce. It CANNOT reliably verify the match is a
    real batch rather than a coincidence -- that would require a shared
    identifier this dataset doesn't have (see project README's "Known
    limitations": this is a deliberately deferred problem, not a bug).
    batch_id is NOT usable for verification here: a genuine batch is
    always a single settlement row with a unique batch_id (so it never
    has "sibling" lines to check), while ordinary same-day settlements
    share a date-derived batch_id with many unrelated transactions --
    checking for shared batch_id would flag real batches as suspicious
    and coincidences as confirmed, which is backwards.

    Given that, ALWAYS treat a "found" result here as a candidate lead,
    never as confirmation. If you find a match, report it as such
    explicitly: "a subset of transactions sums to match settlement line X,
    but this cannot be independently verified as a real batch" -- with
    correspondingly moderate confidence (not above ~0.6), not a confident
    "confirmed_batch". This mirrors the same precision limit already
    measured for the deterministic matcher's identical search (~59-61%
    precision in testing) -- don't claim more certainty here than that
    same algorithm has anywhere else in this project.

    Args:
        internal_txn_id: the transaction to find a batch grouping for
        window_days: how many days around the transaction's date to search
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT transaction_date, amount FROM internal_ledger
            WHERE internal_txn_id = %s
        """, (internal_txn_id,))
        row = cur.fetchone()
        if row is None:
            return {"error": f"no internal transaction found with id {internal_txn_id}"}
        target_date, target_amount = row
        target_amount = float(target_amount)

        start = (target_date - timedelta(days=window_days)).isoformat()
        end = (target_date + timedelta(days=window_days)).isoformat()

        cur.execute("""
            SELECT internal_txn_id, amount FROM internal_ledger
            WHERE transaction_date BETWEEN %s AND %s AND internal_txn_id != %s
        """, (start, end, internal_txn_id))
        candidates = [(r[0], float(r[1])) for r in cur.fetchall()]

        cur.execute("""
            SELECT settlement_line_id, gross_amount FROM network_settlement
            WHERE settlement_date BETWEEN %s AND %s
        """, (start, end))
        settlement_lines = [(r[0], float(r[1])) for r in cur.fetchall()]

    for settlement_line_id, gross_amount in settlement_lines:
        for size in range(1, min(BATCH_SEARCH_MAX_SIZE, len(candidates) + 1)):
            for combo in combinations(candidates, size):
                total = target_amount + sum(c[1] for c in combo)
                if abs(total - gross_amount) <= BATCH_SEARCH_AMOUNT_TOL:
                    return {
                        "found": True,
                        "risk_level": "unverified_numeric_match_only",
                        "settlement_line_id": settlement_line_id,
                        "matched_amount": gross_amount,
                        "batch_members": [internal_txn_id] + [c[0] for c in combo],
                        "note": "Amounts sum correctly. This tool cannot verify this is a real "
                                "batch vs. coincidence -- report as an unverified candidate lead "
                                "with moderate confidence (~0.5-0.6), not as a confirmed batch.",
                    }

    return {"found": False, "note": f"No combination of up to {BATCH_SEARCH_MAX_SIZE} nearby "
            f"transactions (searched {len(candidates)} candidates against "
            f"{len(settlement_lines)} settlement lines) summed to match any settlement line. "
            f"This IS genuine exhaustive evidence -- unlike a 'found' result, an empty result "
            f"from this search is reliable and safe to treat as real support for orphan/unresolved."}


if __name__ == "__main__":
    mcp.run()
