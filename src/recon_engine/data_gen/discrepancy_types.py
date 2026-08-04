"""
The 10 exception types this dataset seeds, modeled on real ACH/card
settlement reconciliation failure modes -- specifically the case where
the external settlement file and the internal ledger share NO common
transaction identifier, forcing entity resolution instead of a key join.

Each internal transaction is assigned exactly one discrepancy_type,
which determines how (and whether) it appears in the settlement file.
This lets us compute ground-truth accuracy later: "did the matching
agent correctly link/flag this transaction?"

Distribution is weighted toward clean_match, because that's reality --
most settlement clears fine. The exceptions are the interesting 10-20%.
"""

from enum import Enum


class DiscrepancyType(str, Enum):
    CLEAN_MATCH = "clean_match"                    # settles normally, 1-2 day lag
    FEE_VARIANCE = "fee_variance"                   # net amount off by an untracked fee
    SPLIT_SETTLEMENT = "split_settlement"           # 1 internal txn -> 2 settlement lines
    BATCHED_SETTLEMENT = "batched_settlement"       # N internal txns -> 1 settlement line
    TIMING_OUTLIER = "timing_outlier"                # settles 10-20 days late
    DUPLICATE_LINE = "duplicate_line"                # settlement line appears twice
    ORPHAN_SETTLEMENT = "orphan_settlement"          # settlement line, no internal record
    ORPHAN_INTERNAL = "orphan_internal"              # internal txn never settles
    HARD_RETURN = "hard_return"                      # NACHA return code, funds reversed
    DESCRIPTOR_MANGLED = "descriptor_mangled"        # everything matches except descriptor text


# (discrepancy_type, weight) -- weights don't need to sum to 1, just relative.
# NOTE: ORPHAN_SETTLEMENT is deliberately NOT in this list. It describes a
# settlement line with no internal source transaction -- it cannot be
# assigned to an internal txn, only injected directly into the settlement
# file (see build_settlement_and_ground_truth's orphan injection step).
DISTRIBUTION: list[tuple[DiscrepancyType, float]] = [
    (DiscrepancyType.CLEAN_MATCH, 55.0),
    (DiscrepancyType.FEE_VARIANCE, 10.0),
    (DiscrepancyType.SPLIT_SETTLEMENT, 6.0),
    (DiscrepancyType.BATCHED_SETTLEMENT, 6.0),
    (DiscrepancyType.TIMING_OUTLIER, 5.0),
    (DiscrepancyType.DUPLICATE_LINE, 4.0),
    (DiscrepancyType.ORPHAN_INTERNAL, 4.0),
    (DiscrepancyType.HARD_RETURN, 4.0),
    (DiscrepancyType.DESCRIPTOR_MANGLED, 2.0),
]

# What each type means for the matcher -- used later when we score the
# investigator agent's proposed resolutions against ground truth.
EXPECTED_MATCH_CARDINALITY = {
    DiscrepancyType.CLEAN_MATCH: "one_to_one",
    DiscrepancyType.FEE_VARIANCE: "one_to_one",
    DiscrepancyType.SPLIT_SETTLEMENT: "one_to_many",
    DiscrepancyType.BATCHED_SETTLEMENT: "many_to_one",
    DiscrepancyType.TIMING_OUTLIER: "one_to_one",
    DiscrepancyType.DUPLICATE_LINE: "one_to_many",   # 1 internal, 2 settlement lines, only 1 valid
    DiscrepancyType.ORPHAN_SETTLEMENT: "unmatched_settlement",
    DiscrepancyType.ORPHAN_INTERNAL: "unmatched_internal",
    DiscrepancyType.HARD_RETURN: "one_to_one_reversed",
    DiscrepancyType.DESCRIPTOR_MANGLED: "one_to_one",
}
