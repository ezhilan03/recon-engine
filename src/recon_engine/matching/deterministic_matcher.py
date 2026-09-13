"""
Deterministic matching baseline.

This is the node that runs BEFORE any LLM in the reconciliation graph.
The whole point of an agentic system here is to spend LLM calls only on
what deterministic rules can't solve -- so this module has to be honest
about its own limits, not stretched to "cheat" toward higher coverage.

Matching strategy, in order:

  1. exact_one_to_one: same account_last4, settlement_date within
     [txn_date, txn_date + WINDOW_DAYS], gross_amount == internal amount
     (within 1 cent). If exactly one candidate -> confident match.

  2. ambiguous: same criteria as (1) but >1 equally good candidate
     (e.g. true duplicate settlement lines). Rules cannot pick between
     them responsibly -- this is a real ambiguity, not a matcher failure.

  3. split_two: no single-line match, but exactly two settlement lines
     (same account_last4, same/adjacent dates) sum to the internal
     amount within 1 cent.

  4. unresolved: none of the above. Routes to the exception queue --
     this is where batching, timing outliers, hard returns, and orphans
     land, by design.

Run:
    uv run python -m recon_engine.matching.evaluate_baseline
"""

import csv
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

WINDOW_DAYS = 5
AMOUNT_TOL = 0.01

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "output"


@dataclass
class InternalRow:
    internal_txn_id: str
    transaction_date: date
    amount: float
    account_last4: str
    merchant_descriptor: str
    nacha_return_code: str
    card_number_masked: str = ""
    mcc: str = ""
    merchant_number: str = ""


@dataclass
class SettlementRow:
    settlement_line_id: str
    settlement_date: date
    gross_amount: float
    fee_amount: float
    net_amount: float
    account_last4: str
    descriptor: str
    batch_id: str
    return_code: str
    card_number_masked: str = ""
    mcc: str = ""
    merchant_number: str = ""


@dataclass
class MatchResult:
    internal_txn_id: str
    match_type: str  # exact_one_to_one | ambiguous | split_two | unresolved
    matched_settlement_ids: list[str] = field(default_factory=list)


def load_data() -> tuple[list[InternalRow], list[SettlementRow]]:
    ledger = []
    with (DATA_DIR / "internal_ledger.csv").open() as f:
        for r in csv.DictReader(f):
            ledger.append(InternalRow(
                internal_txn_id=r["internal_txn_id"],
                transaction_date=date.fromisoformat(r["transaction_date"]),
                amount=float(r["amount"]),
                account_last4=r["account_last4"],
                merchant_descriptor=r["merchant_descriptor"],
                nacha_return_code=r["nacha_return_code"],
                card_number_masked=r.get("card_number_masked", ""),
                mcc=r.get("mcc", ""),
                merchant_number=r.get("merchant_number", ""),
            ))

    settlement = []
    with (DATA_DIR / "network_settlement.csv").open() as f:
        for r in csv.DictReader(f):
            settlement.append(SettlementRow(
                settlement_line_id=r["settlement_line_id"],
                settlement_date=date.fromisoformat(r["settlement_date"]),
                gross_amount=float(r["gross_amount"]),
                fee_amount=float(r["fee_amount"]),
                net_amount=float(r["net_amount"]),
                account_last4=r["account_last4"],
                descriptor=r["descriptor"],
                batch_id=r["batch_id"],
                return_code=r["return_code"],
                card_number_masked=r.get("card_number_masked", ""),
                mcc=r.get("mcc", ""),
                merchant_number=r.get("merchant_number", ""),
            ))
    return ledger, settlement


def _in_window(txn_date: date, settle_date: date) -> bool:
    delta = (settle_date - txn_date).days
    return 0 <= delta <= WINDOW_DAYS


def _money(value: float | Decimal) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Matching amounts must be finite")
    return amount


def _amounts_equal(a: float | Decimal, b: float | Decimal) -> bool:
    return abs(_money(a) - _money(b)) <= Decimal(str(AMOUNT_TOL))


def run_matcher(
    ledger: list[InternalRow],
    settlement: list[SettlementRow],
) -> list[MatchResult]:
    """Generate candidates first, then reserve only uncontested allocations.

    Ambiguous result IDs are evidence, not allocations. A settlement mentioned
    by competing transactions is routed to review for all of them, rather than
    allowing input order to choose a winner. Exact candidates retain precedence
    over splits. This is a conservative baseline, not a global optimiser or a
    cross-run database reservation system.
    """
    if len({t.internal_txn_id for t in ledger}) != len(ledger):
        raise ValueError("Duplicate internal transaction IDs; deduplicate ingestion first")
    if len({s.settlement_line_id for s in settlement}) != len(settlement):
        raise ValueError("Duplicate settlement IDs; deduplicate ingestion first")
    for t in ledger:
        _money(t.amount)
    for s in settlement:
        _money(s.gross_amount)
    # index settlement lines by account_last4 for fast candidate lookup
    by_account: dict[str, list[SettlementRow]] = {}
    for s in settlement:
        by_account.setdefault(s.account_last4, []).append(s)

    results = []
    for txn in ledger:
        candidates = by_account.get(txn.account_last4, [])
        window_candidates = [c for c in candidates if _in_window(txn.transaction_date, c.settlement_date)]

        # Step 1 & 2: exact single-line match
        exact_matches = [c for c in window_candidates if _amounts_equal(c.gross_amount, txn.amount)]
        if len(exact_matches) == 1:
            results.append(MatchResult(txn.internal_txn_id, "exact_one_to_one",
                                        [exact_matches[0].settlement_line_id]))
            continue
        if len(exact_matches) > 1:
            results.append(MatchResult(txn.internal_txn_id, "ambiguous",
                                        [c.settlement_line_id for c in exact_matches]))
            continue

        # Step 3: two-line split that sums to the amount
        split_pairs = []
        for i in range(len(window_candidates)):
            for j in range(i + 1, len(window_candidates)):
                left, right = window_candidates[i], window_candidates[j]
                if abs((left.settlement_date - right.settlement_date).days) > 1:
                    continue
                pair_sum = _money(left.gross_amount) + _money(right.gross_amount)
                if _amounts_equal(pair_sum, txn.amount):
                    split_pairs.append((left.settlement_line_id, right.settlement_line_id))
        if split_pairs:
            results.append(MatchResult(
                txn.internal_txn_id,
                "split_two" if len(split_pairs) == 1 else "ambiguous",
                sorted({sid for pair in split_pairs for sid in pair}),
            ))
            continue

        # Step 4: nothing worked
        results.append(MatchResult(txn.internal_txn_id, "unresolved", []))

    claimants: dict[str, set[str]] = {}
    for result in results:
        result.matched_settlement_ids.sort()
        for sid in result.matched_settlement_ids:
            claimants.setdefault(sid, set()).add(result.internal_txn_id)
    for result in results:
        if any(len(claimants[sid]) > 1 for sid in result.matched_settlement_ids):
            result.match_type = "ambiguous"
    return results
