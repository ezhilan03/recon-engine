"""Opt-in disaster recovery drill using a disposable local PostgreSQL container."""
import os
import subprocess
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tests import test_database as fixtures
DSN = fixtures.DSN
from recon_engine.db.allocations import reserve
from recon_engine.db.loader import load_dataset


@unittest.skipUnless(DSN and os.environ.get('RECON_TEST_PG_CONTAINER'),
                     'Set explicit test database and disposable PG container for restore drill')
class BackupRestoreTests(unittest.TestCase):
    def test_restore_retains_ingestion_and_allocation_invariants(self):
        fixture = fixtures.DatabaseTests()
        fixture.setUp()
        target = 'restore_' + uuid.uuid4().hex
        container = os.environ['RECON_TEST_PG_CONTAINER']
        created = False
        try:
            fixture.load()
            with fixture.conn.transaction():
                reserve(fixture.conn, ['t3', 't4'], ['s1'])
            dump = subprocess.run(['docker', 'exec', container, 'pg_dump', '-U', 'postgres',
                                   '-d', 'recon', '-n', fixture.schema, '-Fc'],
                                  capture_output=True, check=True, timeout=30).stdout
            with psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(target)))
                created = True
            subprocess.run(['docker', 'exec', '-i', container, 'pg_restore', '-U', 'postgres',
                            '-d', target, '--exit-on-error'], input=dump, capture_output=True,
                           check=True, timeout=30)
            with psycopg.connect(make_conninfo(DSN, dbname=target), autocommit=True) as restored:
                restored.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(fixture.schema)))
                self.assertTrue(load_dataset(restored, fixture.directory)['replayed'])
                self.assertEqual(restored.execute('SELECT count(*) FROM allocated_transactions').fetchone()[0], 2)
                with self.assertRaises(ValueError), restored.transaction():
                    reserve(restored, ['t1'], ['s1'])
                self.assertEqual(restored.execute('SELECT count(*) FROM allocated_settlements').fetchone()[0], 1)
        finally:
            if created:
                with psycopg.connect(DSN, autocommit=True) as admin:
                    admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(target)))
            fixture.tearDown()
