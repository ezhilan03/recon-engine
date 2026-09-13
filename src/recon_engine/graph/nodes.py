"""
Graph nodes for the reconciliation pipeline, pre-agent.

Node order:
  build_exception_queue -> classify_rule_based -> resolve_batches -> END

Everything here is still deterministic -- no LLM call. The point is to
shrink the exception queue as far as rules can take it before any agent
touches it, same principle as the matcher itself.
"""

from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations

from recon_engine.graph.state import ExceptionCase, GraphState, SettlementLineDict
from recon_engine.matching.deterministic_matcher import (
    InternalRow,
    SettlementRow,
    run_matcher,
)

WIDE_WINDOW_DAYS = 25
AMOUNT_TOL = 0.01
MAX_BATCH_SIZE = 4


def _unwrap_exception(e: BaseException) -> str:
    """Python 3.11's TaskGroup wraps failures in a BaseExceptionGroup, whose
    str() is just a generic 'unhandled errors in a TaskGroup (N sub-
    exception)' message -- it hides the actual underlying error. This walks
    into .exceptions (recursively, since groups can nest) and returns the
    real root-cause message(s) instead."""
    if isinstance(e, BaseExceptionGroup):
        parts = [_unwrap_exception(sub) for sub in e.exceptions]
        return " | ".join(parts)
    return f"{type(e).__name__}: {e}"


def build_exception_queue(ledger: list[InternalRow], settlement: list[SettlementRow]) -> GraphState:
    """Runs the existing deterministic matcher, then builds graph state from
    whatever it couldn't resolve (unresolved + ambiguous), plus the set of
    settlement lines no result claimed."""
    results = run_matcher(ledger, settlement)
    ledger_by_id = {t.internal_txn_id: t for t in ledger}

    claimed_ids = {sid for r in results for sid in r.matched_settlement_ids}
    unclaimed = [s for s in settlement if s.settlement_line_id not in claimed_ids]

    exceptions: list[ExceptionCase] = []
    for r in results:
        if r.match_type in ("unresolved", "ambiguous"):
            txn = ledger_by_id[r.internal_txn_id]
            # Bug found in session testing: the matcher already knows which
            # specific settlement lines it considers ambiguous candidates
            # (r.matched_settlement_ids), but that was being discarded here
            # (evidence hardcoded to {}). The investigator was then
            # investigating "duplicate_needs_review" cases with zero
            # information about which two lines were actually in question --
            # it checked the wrong side of the problem (internal transaction
            # history instead of the flagged settlement lines) and drew a
            # confident but wrong conclusion from real but irrelevant data.
            initial_evidence = {}
            if r.matched_settlement_ids:
                initial_evidence["matcher_candidate_settlement_ids"] = r.matched_settlement_ids

            exceptions.append(ExceptionCase(
                internal_txn_id=txn.internal_txn_id,
                transaction_date=txn.transaction_date.isoformat(),
                amount=txn.amount,
                account_last4=txn.account_last4,
                merchant_descriptor=txn.merchant_descriptor,
                nacha_return_code=txn.nacha_return_code,
                original_match_type=r.match_type,
                classification=None,
                evidence=initial_evidence,
            ))

    unclaimed_dicts: list[SettlementLineDict] = [
        SettlementLineDict(
            settlement_line_id=s.settlement_line_id,
            settlement_date=s.settlement_date.isoformat(),
            gross_amount=s.gross_amount,
            fee_amount=s.fee_amount,
            net_amount=s.net_amount,
            account_last4=s.account_last4,
            descriptor=s.descriptor,
            batch_id=s.batch_id,
            return_code=s.return_code,
        )
        for s in unclaimed
    ]

    return GraphState(exceptions=exceptions, unclaimed_settlement=unclaimed_dicts, bucket_counts={})


def classify_rule_based(state: GraphState) -> GraphState:
    """First pass: cheap, deterministic classification. No search yet --
    just checking fields we already have."""
    unclaimed_by_descriptor: dict[str, list[SettlementLineDict]] = {}
    for s in state["unclaimed_settlement"]:
        unclaimed_by_descriptor.setdefault(s["descriptor"], []).append(s)

    for case in state["exceptions"]:
        if case["nacha_return_code"]:
            case["classification"] = "hard_return"
            case["evidence"] = {"nacha_return_code": case["nacha_return_code"]}
            continue

        if case["original_match_type"] == "ambiguous":
            # matcher already found the duplicate pair; this needs a human
            # decision on which line is real, not more searching
            case["classification"] = "duplicate_needs_review"
            continue

        txn_date = date.fromisoformat(case["transaction_date"])
        same_account_wide = [
            s for s in state["unclaimed_settlement"]
            if s["account_last4"] == case["account_last4"]
            and 0 <= (date.fromisoformat(s["settlement_date"]) - txn_date).days <= WIDE_WINDOW_DAYS
        ]
        exact_wide_matches = [s for s in same_account_wide if abs(s["gross_amount"] - case["amount"]) <= AMOUNT_TOL]
        if len(exact_wide_matches) == 1:
            case["classification"] = "timing_outlier"
            case["evidence"] = {"settlement_line_id": exact_wide_matches[0]["settlement_line_id"],
                                 "days_late": (date.fromisoformat(exact_wide_matches[0]["settlement_date"]) - txn_date).days}
            continue

        # no settlement line anywhere shares this txn's own account_last4 --
        # the signature of a batch component (the settlement line kept a
        # *different* member's last4) rather than a truly missing txn.
        # NOTE: we deliberately do NOT use descriptor as a batch signal --
        # batch members don't reliably share a descriptor (the settlement
        # line's descriptor field just borrows the first member's), so
        # descriptor-based grouping produces false positives.
        if not same_account_wide:
            case["classification"] = "needs_batch_search"
            continue

        case["classification"] = "needs_llm_investigation"

    return state


def resolve_batches(state: GraphState) -> GraphState:
    """Second pass: for cases flagged needs_batch_search, try to find a
    subset of those cases whose amounts sum to one unclaimed settlement
    line's gross_amount. Batch members don't share a descriptor or
    account_last4 with each other, so the only usable prefilter is date
    proximity to the settlement line -- everything else is exhaustive
    subset-sum, which is why size is capped at MAX_BATCH_SIZE.

    Whatever doesn't resolve to a batch after this search is genuinely
    ambiguous: it might be a real orphan_internal (never going to settle),
    or a batch our size/window limits missed. We don't guess which --
    that call goes to the agent/human, not asserted here."""
    batch_candidates = [c for c in state["exceptions"] if c["classification"] == "needs_batch_search"]
    if not batch_candidates:
        return state

    # Enumerate candidates before claiming anything. Amount-only candidates are
    # proposals for explicit review, never operational resolutions.
    candidates = []
    searched = 0
    for line in sorted(state["unclaimed_settlement"], key=lambda s: s["settlement_line_id"]):
        eligible = [c for c in batch_candidates
                    if 0 <= (date.fromisoformat(line["settlement_date"]) -
                             date.fromisoformat(c["transaction_date"])).days <= WIDE_WINDOW_DAYS]
        for size in range(2, min(MAX_BATCH_SIZE, len(eligible)) + 1):
            for combo in combinations(eligible, size):
                searched += 1
                if searched > 10000:
                    for c in batch_candidates:
                        c["classification"] = "batch_search_limit_review"
                    return state
                if abs(sum(Decimal(str(c["amount"])) for c in combo) -
                       Decimal(str(line["gross_amount"]))) <= Decimal("0.01"):
                    candidates.append((sorted(c["internal_txn_id"] for c in combo), line))
    for c in batch_candidates:
        c["classification"] = "no_batch_match_found"
    for members, line in candidates:
        competing = [g for g, other in candidates
                     if set(g) & set(members) or other["settlement_line_id"] == line["settlement_line_id"]]
        if len(competing) != 1:
            for c in batch_candidates:
                if c["internal_txn_id"] in members:
                    c["classification"] = "ambiguous_batch_review"
            continue
        group = {"transaction_ids": members, "settlement_ids": [line["settlement_line_id"]]}
        for c in batch_candidates:
            if c["internal_txn_id"] in members:
                c["classification"] = "batch_pending_review"
                c["evidence"]["allocation_group"] = group
                c["evidence"]["resolution_proposal"] = {
                    "internal_txn_id": c["internal_txn_id"], "resolution_type": "confirmed_batch",
                    "matched_settlement_line_ids": group["settlement_ids"], "confidence": 0.5,
                    "reasoning": "Complete group balances by amount and date; business identity requires human verification.",
                    "requires_human_approval": True,
                }

    return state


def summarize(state: GraphState) -> GraphState:
    counts: dict[str, int] = {}
    for case in state["exceptions"]:
        label = case["classification"] or "unclassified"
        counts[label] = counts.get(label, 0) + 1
    state["bucket_counts"] = counts
    return state


# Labels from the rule-based pass that genuinely need the agent --
# either no structural signal existed at all, or a candidate was found
# but couldn't be trusted without verification (see the 59%/41% accuracy
# split from Session 1's batch resolution).
NEEDS_AGENT = {"needs_llm_investigation", "duplicate_needs_review", "no_batch_match_found"}


async def investigate(state: GraphState) -> GraphState:
    """Runs the investigator agent on every case flagged as needing it.
    Stores free-text findings on each case's evidence dict under
    'investigation_findings' -- structuring happens in propose_resolutions.

    A failure on one case (rate limit, billing, transient API error)
    doesn't abort the whole batch -- it's recorded on that case so
    whatever already succeeded isn't thrown away.

    Set INVESTIGATE_LIMIT=N in the environment to cap how many cases get
    investigated in a single run -- useful while debugging so a bug
    doesn't burn through the full queue's worth of API calls/quota before
    you find it."""
    import os

    from recon_engine.agent.investigator import investigate_case

    limit = int(os.environ.get("INVESTIGATE_LIMIT", "0"))
    processed = 0
    eligible = [c for c in state["exceptions"] if c["classification"] in NEEDS_AGENT]
    print(f"[investigate] {len(eligible)} cases eligible, limit={limit or 'none'}")

    for case in eligible:
        if limit and processed >= limit:
            case["evidence"]["investigation_skipped"] = f"INVESTIGATE_LIMIT={limit} reached"
            continue
        print(f"[investigate] ({processed + 1}/{limit or len(eligible)}) starting {case['internal_txn_id']} "
              f"(${case['amount']:.2f}, {case['classification']})...")
        try:
            findings = await investigate_case(case)
            case["evidence"]["investigation_findings"] = findings
            processed += 1
            print(f"[investigate] ({processed}/{limit or len(eligible)}) OK {case['internal_txn_id']} "
                  f"-- {len(findings)} chars of findings")
        except Exception as e:
            case["evidence"]["investigation_error"] = _unwrap_exception(e)
            processed += 1
            print(f"[investigate] ({processed}/{limit or len(eligible)}) FAILED {case['internal_txn_id']} "
                  f"-- {case['evidence']['investigation_error']}")

    return state


async def propose_resolutions(state: GraphState) -> GraphState:
    """Runs the Guardrails-validated proposer on every case that has
    investigation findings. A failure on one case doesn't abort the whole
    batch -- it's recorded on that case so the run still completes and
    reports what it can."""
    from recon_engine.agent.proposer import propose_resolution

    to_process = [c for c in state["exceptions"] if c["evidence"].get("investigation_findings")]
    print(f"[propose] {len(to_process)} cases with findings to structure")

    for case in to_process:
        try:
            proposal = propose_resolution(case["internal_txn_id"], case["evidence"]["investigation_findings"])
            case["evidence"]["resolution_proposal"] = proposal.model_dump()
            print(f"[propose] OK {case['internal_txn_id']} -- {proposal.resolution_type}, "
                  f"confidence={proposal.confidence:.2f}")
        except Exception as e:
            case["evidence"]["proposal_error"] = _unwrap_exception(e)
            print(f"[propose] FAILED {case['internal_txn_id']} -- {case['evidence']['proposal_error']}")

    return state


# Dollar threshold enforced in code, independent of the proposer's own
# confidence-based requires_human_approval flag. The model's judgment is
# one signal; the threshold is a hard rule that can't be reasoned around.
HIGH_VALUE_THRESHOLD = 100.00


def human_approval(state: GraphState) -> GraphState:
    """For every case with a resolution proposal that needs sign-off
    (proposer flagged it OR the amount crosses HIGH_VALUE_THRESHOLD --
    either condition is sufficient), pause the graph via interrupt() and
    wait for a human decision before marking it resolved.

    NOTE: this node is NOT async. interrupt() relies on LangGraph's
    replay mechanics (the node re-runs from the top on each resume, with
    already-answered interrupts returning their cached value instantly)
    -- mixing that with async/await adds complexity this project doesn't
    need yet, so this node stays sync even though others aren't."""
    from langgraph.types import interrupt

    reviewed_groups = {}
    for case in state["exceptions"]:
        proposal = case["evidence"].get("resolution_proposal")
        if not proposal:
            continue

        group = case["evidence"].get("allocation_group")
        group_key = (tuple(group["transaction_ids"]), tuple(group["settlement_ids"])) if group else None
        if group_key in reviewed_groups:
            case["evidence"]["human_decision"] = reviewed_groups[group_key]
            continue
        low_confidence = proposal["confidence"] < 0.85
        needs_approval = (proposal["requires_human_approval"] or low_confidence
                          or case["amount"] >= HIGH_VALUE_THRESHOLD
                          or proposal["resolution_type"] == "confirmed_batch")
        if not needs_approval:
            case["evidence"]["human_decision"] = "auto_approved"
            continue

        decision = interrupt({
            "internal_txn_id": case["internal_txn_id"],
            "amount": case["amount"],
            "allocation_group": group,
            "group_members": [
                {k: member[k] for k in ("internal_txn_id", "amount", "transaction_date", "account_last4")}
                for member in state["exceptions"]
                if group and member["internal_txn_id"] in group["transaction_ids"]
            ],
            "resolution_type": proposal["resolution_type"],
            "matched_settlement_line_ids": proposal["matched_settlement_line_ids"],
            "confidence": proposal["confidence"],
            "reasoning": proposal["reasoning"],
            "flagged_reason": (
                "low_confidence" if proposal["requires_human_approval"] or low_confidence
                else "high_value_threshold"
            ),
        })
        if decision not in ("approved", "rejected"):
            raise ValueError("Human decision must be approved or rejected")
        case["evidence"]["human_decision"] = decision
        if group_key:
            reviewed_groups[group_key] = decision

    return state
