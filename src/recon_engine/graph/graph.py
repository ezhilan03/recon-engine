"""
The reconciliation graph, built with raw StateGraph -- deliberately not
langgraph.prebuilt (deprecated as of LangGraph 1.0; functionality moved
to langchain.agents). Every node here is a plain function so each edge
in the pipeline is explicit and explainable.

Current graph (pre-agent):

    START -> classify -> resolve_batches -> summarize -> END

classify and resolve_batches operate on state built by
build_exception_queue(), which isn't itself a graph node (it needs the
raw ledger/settlement lists as input, which come from the DB, not from
graph state) -- it's the seam between "load data" and "run the graph."

Run:
    uv run python -m recon_engine.graph.run_classification
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from recon_engine.graph.nodes import (
    classify_rule_based,
    human_approval,
    investigate,
    propose_resolutions,
    resolve_batches,
    summarize,
)
from recon_engine.graph.state import GraphState


def build_graph():
    graph = StateGraph(GraphState)

    graph.add_node("classify", classify_rule_based)
    graph.add_node("resolve_batches", resolve_batches)
    graph.add_node("investigate", investigate)
    graph.add_node("propose_resolutions", propose_resolutions)
    graph.add_node("human_approval", human_approval)
    graph.add_node("summarize", summarize)

    graph.add_edge(START, "classify")
    graph.add_edge("classify", "resolve_batches")
    graph.add_edge("resolve_batches", "investigate")
    graph.add_edge("investigate", "propose_resolutions")
    graph.add_edge("propose_resolutions", "human_approval")
    graph.add_edge("human_approval", "summarize")
    graph.add_edge("summarize", END)

    # interrupt() requires a checkpointer -- it's how LangGraph persists
    # state across the pause. InMemorySaver is fine for this single-process
    # CLI demo; a production deployment would use a Postgres-backed
    # checkpointer (langgraph-checkpoint-postgres) so an interrupt survives
    # a process restart, not just a pause within one run.
    return graph.compile(checkpointer=InMemorySaver())
