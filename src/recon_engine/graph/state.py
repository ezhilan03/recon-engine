"""
State schema for the reconciliation graph.

Design note: this state holds the *whole batch* of exception cases as a
list, not one case per graph invocation. That's a simplification for this
stage (pre-classification + deterministic batch resolution, no LLM yet).
Once we add the investigator agent + human-in-the-loop interrupt, we'll
likely switch to LangGraph's Send() API to fan out one sub-run per case
so each can pause/resume independently -- flagging that now so it's not
a surprise later.
"""

from typing import TypedDict, NotRequired


class ExceptionCase(TypedDict):
    internal_txn_id: str
    transaction_date: str  # ISO date string
    amount: float
    account_last4: str
    merchant_descriptor: str
    nacha_return_code: str
    original_match_type: str  # "unresolved" | "ambiguous"
    classification: str | None
    evidence: dict


class SettlementLineDict(TypedDict):
    settlement_line_id: str
    settlement_date: str
    gross_amount: float
    fee_amount: float
    net_amount: float
    account_last4: str
    descriptor: str
    batch_id: str
    return_code: str


class GraphState(TypedDict):
    run_signature: NotRequired[str]
    exceptions: list[ExceptionCase]
    unclaimed_settlement: list[SettlementLineDict]
    bucket_counts: dict[str, int]
