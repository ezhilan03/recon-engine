"""
Tests the human_approval node's interrupt()/resume mechanics in isolation,
using fake pre-built state instead of a real investigation run. This
needs no DATABASE_URL and no ANTHROPIC_API_KEY -- it's purely testing
LangGraph's pause/resume behavior, which is easy to get wrong (the "node
re-runs from the top on resume" semantics are non-obvious) and worth
verifying independent of the rest of the pipeline.

Run:
    uv run python -m tests.test_human_approval
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from recon_engine.graph.nodes import human_approval
from recon_engine.graph.state import GraphState


def build_test_graph():
    graph = StateGraph(GraphState)
    graph.add_node("human_approval", human_approval)
    graph.add_edge(START, "human_approval")
    graph.add_edge("human_approval", END)
    return graph.compile(checkpointer=InMemorySaver())


def fake_state() -> GraphState:
    return GraphState(
        exceptions=[
            {
                "internal_txn_id": "TXN-FAKE-LOWVALUE",
                "transaction_date": "2026-06-01",
                "amount": 42.00,  # below threshold, high confidence -> auto-approved
                "account_last4": "0000",
                "merchant_descriptor": "TEST",
                "nacha_return_code": "",
                "original_match_type": "unresolved",
                "classification": "needs_llm_investigation",
                "evidence": {
                    "resolution_proposal": {
                        "internal_txn_id": "TXN-FAKE-LOWVALUE",
                        "resolution_type": "confirmed_match",
                        "matched_settlement_line_ids": ["SETL-FAKE-1"],
                        "confidence": 0.95,
                        "reasoning": "test case, high confidence",
                        "requires_human_approval": False,
                    },
                },
            },
            {
                "internal_txn_id": "TXN-FAKE-HIGHVALUE",
                "transaction_date": "2026-06-01",
                "amount": 250.00,  # above threshold -> needs approval regardless of confidence
                "account_last4": "1111",
                "merchant_descriptor": "TEST",
                "nacha_return_code": "",
                "original_match_type": "unresolved",
                "classification": "needs_llm_investigation",
                "evidence": {
                    "resolution_proposal": {
                        "internal_txn_id": "TXN-FAKE-HIGHVALUE",
                        "resolution_type": "confirmed_batch",
                        "matched_settlement_line_ids": ["SETL-FAKE-2", "SETL-FAKE-3"],
                        "confidence": 0.91,
                        "reasoning": "test case, high value forces review",
                        "requires_human_approval": False,
                    },
                },
            },
        ],
        unclaimed_settlement=[],
        bucket_counts={},
    )


def main() -> None:
    app = build_test_graph()
    config = {"configurable": {"thread_id": "test-thread-1"}}

    result = app.invoke(fake_state(), config=config)

    if "__interrupt__" in result:
        interrupt_payload = result["__interrupt__"][0].value
        print("Graph paused. Interrupt payload:")
        print(" ", interrupt_payload)
        assert interrupt_payload["internal_txn_id"] == "TXN-FAKE-HIGHVALUE", \
            "expected the high-value case to trigger the interrupt, not the low-value one"
        assert interrupt_payload["flagged_reason"] == "high_value_threshold", \
            "expected flagged_reason to be high_value_threshold since confidence was high"
        print("PASS: low-value case auto-approved, high-value case correctly paused the graph.\n")

        print("Resuming with a simulated human approval...")
        final = app.invoke(Command(resume="approved"), config=config)

        low_case = next(c for c in final["exceptions"] if c["internal_txn_id"] == "TXN-FAKE-LOWVALUE")
        high_case = next(c for c in final["exceptions"] if c["internal_txn_id"] == "TXN-FAKE-HIGHVALUE")
        assert low_case["evidence"]["human_decision"] == "auto_approved"
        assert high_case["evidence"]["human_decision"] == "approved"
        print("PASS: resume correctly applied the human decision to the paused case,")
        print("      and the auto-approved case's earlier decision was preserved.")
    else:
        raise AssertionError("Expected the high-value case to trigger an interrupt")


if __name__ == "__main__":
    main()
