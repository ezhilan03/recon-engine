"""Restart-safe graph sessions with an exclusive lease per run ID."""
from contextlib import asynccontextmanager

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from recon_engine.graph.graph import build_graph


@asynccontextmanager
async def session(dsn, run_id, builder=build_graph):
    if not run_id or len(run_id) > 200:
        raise ValueError("A stable run ID of 1-200 characters is required")
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as lease:
        result = await lease.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 7131303))", (run_id,))
        if not (await result.fetchone())[0]:
            raise ValueError("This run is already active in another process")
        try:
            async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
                await saver.setup()
                yield builder(checkpointer=saver), {"configurable": {"thread_id": run_id}}
        finally:
            await lease.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 7131303))", (run_id,))


async def start_or_resume(app, config, initial_state, signature):
    snapshot = await app.aget_state(config)
    if snapshot.values:
        if snapshot.values.get("run_signature") != signature:
            raise ValueError("Run inputs or implementation changed; use a new run ID")
        interrupts = tuple(i for task in snapshot.tasks for i in task.interrupts)
        if interrupts:
            return {**snapshot.values, "__interrupt__": interrupts}
        if not snapshot.next:
            return snapshot.values
        return await app.ainvoke(None, config=config)
    return await app.ainvoke({**initial_state, "run_signature": signature}, config=config)
