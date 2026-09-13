import asyncio
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command

from recon_engine.agent import proposer, investigator
from recon_engine.agent.cache import read, write
from recon_engine.graph.nodes import human_approval, resolve_batches
from recon_engine.graph.runtime import session, start_or_resume
from recon_engine.graph.state import GraphState
from tests.test_human_approval import fake_state


def approval_graph(checkpointer):
    graph = StateGraph(GraphState)
    graph.add_node('review', human_approval)
    graph.add_edge(START, 'review')
    graph.add_edge('review', END)
    return graph.compile(checkpointer=checkpointer)


class CacheTests(unittest.TestCase):
    def test_changed_findings_model_and_source_invalidate(self):
        self.assertNotEqual(proposer._cache_path('t', 'old'), proposer._cache_path('t', 'new'))
        original = proposer._cache_path('t', 'old')
        with patch.object(proposer, 'PROPOSER_MODEL', 'other'):
            self.assertNotEqual(original, proposer._cache_path('t', 'old'))
        self.assertNotEqual(investigator._cache_path({'internal_txn_id': 't'}, 'v1'),
                            investigator._cache_path({'internal_txn_id': 't'}, 'v2'))
        self.assertNotEqual(investigator._cache_path({'amount': 1}, 'v1'),
                            investigator._cache_path({'amount': 2}, 'v1'))

    def test_partial_cache_is_ignored_and_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cache.json'
            path.write_text('{partial')
            self.assertEqual(read(path), {})
            write(path, {'findings': 'complete'})
            self.assertEqual(read(path), {'findings': 'complete'})
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)


class BatchReviewTests(unittest.TestCase):
    def state(self):
        cases = []
        for tid, amount in [('a', 60), ('b', 40)]:
            c = copy.deepcopy(fake_state()['exceptions'][0])
            c.update(internal_txn_id=tid, amount=amount, classification='needs_batch_search', evidence={})
            cases.append(c)
        return {'exceptions': cases, 'bucket_counts': {}, 'unclaimed_settlement': [
            {'settlement_line_id': 's', 'settlement_date': '2026-06-02', 'gross_amount': 100}]}

    def test_complete_group_is_reviewed_once_and_rejected_together(self):
        state = resolve_batches(self.state())
        calls = []
        def reject(payload):
            calls.append(payload)
            return 'rejected'
        with patch('langgraph.types.interrupt', reject):
            result = human_approval(state)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['allocation_group']['transaction_ids'], ['a', 'b'])
        self.assertTrue(all(c['evidence']['human_decision'] == 'rejected' for c in result['exceptions']))

    def test_competing_settlements_never_select_first(self):
        state = self.state()
        state['unclaimed_settlement'].append({**state['unclaimed_settlement'][0], 'settlement_line_id': 'other'})
        result = resolve_batches(state)
        self.assertTrue(all(c['classification'] == 'ambiguous_batch_review' for c in result['exceptions']))
        self.assertFalse(any('resolution_proposal' in c['evidence'] for c in result['exceptions']))


@unittest.skipUnless(os.environ.get('RECON_TEST_DATABASE_URL'), 'Explicit disposable database required')
class CheckpointTests(unittest.TestCase):
    def test_process_restart_preserves_pending_approval_and_rejects_changed_input(self):
        base = os.environ['RECON_TEST_DATABASE_URL']
        schema = 'checkpoint_' + uuid.uuid4().hex
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        dsn = make_conninfo(base, options=f'-c search_path={schema}')
        try:
            # The writer process really exits before a new connection resumes it.
            child = '''import asyncio, os
from tests.test_agent_reliability import approval_graph, fake_state
from recon_engine.graph.runtime import session, start_or_resume
async def run():
    async with session(os.environ['CHECKPOINT_TEST_DSN'], 'restart', approval_graph) as (app, config):
        result = await start_or_resume(app, config, fake_state(), 'version-1')
        assert '__interrupt__' in result
asyncio.run(run())
'''
            proc = subprocess.run([sys.executable, '-c', child], env={**os.environ, 'CHECKPOINT_TEST_DSN': dsn},
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            async def resume():
                async with session(dsn, 'restart', approval_graph) as (app, config):
                    with self.assertRaises(ValueError):
                        await start_or_resume(app, config, fake_state(), 'changed')
                    result = await start_or_resume(app, config, fake_state(), 'version-1')
                    self.assertIn('__interrupt__', result)
                    with self.assertRaises(ValueError):
                        async with session(dsn, 'restart', approval_graph):
                            pass
                    result = await app.ainvoke(Command(resume='approved'), config=config)
                    self.assertEqual(result['exceptions'][1]['evidence']['human_decision'], 'approved')
                async with session(dsn, 'restart', approval_graph) as (app, config):
                    done = await start_or_resume(app, config, fake_state(), 'version-1')
                    self.assertNotIn('__interrupt__', done)
                    self.assertEqual(done['exceptions'][1]['evidence']['human_decision'], 'approved')
            asyncio.run(resume())
        finally:
            with psycopg.connect(base, autocommit=True) as conn:
                conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
