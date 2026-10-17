"""Task context, migration, access declarations and transactional connections."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover.ledger import connect, migrate, ops, schema_version
from drover.ledger.schema import MIGRATIONS


class TaskContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = connect(Path(self.tmp.name) / 'ledger.db')
        self.addCleanup(self.c.close)

    def task(self, title='A', **kw):
        return ops.create_task(self.c, title, 'p', **kw)

    def test_migration_backfills_and_checks_kind(self):
        old = connect(Path(self.tmp.name) / 'old.db', migrate_db=False)
        self.addCleanup(old.close)
        migrate(old, MIGRATIONS[:3])
        old.execute("INSERT INTO tasks(id,repo,title,rank,status,created,updated) VALUES('T-old','p','old',1,'proposed','','')")
        migrate(old)
        t = ops.get_task(old, 'T-old')
        self.assertEqual(t['kind'], 'implementation')
        self.assertIs(t['write'], True)
        self.assertEqual(t['read_access'], ['p'])
        with self.assertRaises(sqlite3.IntegrityError):
            old.execute("UPDATE tasks SET kind='oops'")
        self.assertEqual(migrate(old), schema_version(old))

    def test_defaults_and_description_alias(self):
        for kind in ('research', 'investigation', 'review', 'implementation', 'fix', 'docs', 'mechanical'):
            t = self.task(kind, kind=kind, description='**Context**')
            self.assertEqual(t['description'], t['goal'])
            self.assertEqual(t['goal'], '**Context**')
            self.assertEqual(t['write'], kind not in ('research', 'investigation', 'review'))
            self.assertEqual(t['read_access'], ['p@main'])
        self.assertTrue(self.task(kind='research', write=True)['write'])
        t = ops.update_task(self.c, t['id'], kind='review')
        self.assertTrue(t['write'])  # explicit/persisted access survives kind edits
        with self.assertRaises(ValueError):
            self.task(kind='oops')

    @patch('drover.repos.load_all', return_value={'p': {'default_branch': 'trunk'}})
    def test_read_access_validation(self, _):
        self.assertEqual(self.task()['read_access'], ['p@trunk'])
        self.assertEqual(self.task(read_access=['p', 'p@topic/x'])['read_access'], ['p', 'p@topic/x'])
        for entries in (['missing'], ['p@../secret'], ['p@-bad'], ['p@'], 'p', [3]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.task(read_access=entries)

    def test_connections_are_symmetric_and_atomic(self):
        a, b, c = [self.task(s)['id'] for s in ('a', 'b', 'c')]
        ops.update_task(self.c, a, deps=[{'depends_on': b, 'kind': 'waits-for'}], related=[c])
        self.assertEqual(ops.deps_of(self.c, a), [{'depends_on': b, 'kind': 'waits-for'}])
        self.assertEqual(ops.related_of(self.c, c), [a])
        self.assertEqual(ops.dependents_of(self.c, b), [a])
        with self.assertRaises(ValueError):
            ops.update_task(self.c, b, title='bad', deps=[a])
        self.assertEqual(ops.get_task(self.c, b)['title'], 'b')
        with self.assertRaises(ops.NotFound):
            ops.update_task(self.c, a, title='bad', deps=[], related=['T-missing'])
        self.assertEqual(ops.deps_of(self.c, a)[0]['depends_on'], b)
        self.assertEqual(ops.related_of(self.c, a), [c])
        with self.assertRaises(ValueError):
            ops.update_task(self.c, a, related=[a])
        ops.update_task(self.c, c, related=[])
        self.assertEqual(ops.related_of(self.c, a), [])

    def test_failed_create_leaves_no_task_or_event(self):
        before = len(ops.list_tasks(self.c))
        with self.assertRaises(ops.NotFound):
            self.task(related=['T-missing'])
        self.assertEqual(len(ops.list_tasks(self.c)), before)
        self.assertEqual(ops.events(self.c), [])

    def test_cli_and_importer_accept_context_and_reject_invalid_kind(self):
        import contextlib
        import io
        import json
        from drover import importer
        from drover.ledger import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['--db', str(Path(self.tmp.name) / 'cli.db'), '--json',
                             'add', 'task', 'CLI research', '--repo', 'p', '--kind', 'research',
                             '--description', '**Read**', '--branch', 'topic'])
        self.assertEqual(code, 0)
        task = json.loads(out.getvalue())
        self.assertEqual(task['kind'], 'research')
        self.assertEqual(task['description'], '**Read**')
        self.assertFalse(task['write'])
        self.assertEqual(task['branch'], 'topic')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.parser().parse_args(['add', 'task', 'Bad', '--kind', 'invalid'])
        importer.run_import(self.c, {'tasks': [dict(title='Imported', repo='p', id='i', kind='docs',
                                                  description='Markdown', write=False)]})
        made = ops.list_tasks(self.c)[0]
        self.assertEqual(made['kind'], 'docs')
        self.assertFalse(made['write'])
        self.assertEqual(made['description'], 'Markdown')
        with self.assertRaises(ValueError):
            importer.run_import(self.c, {'tasks': [dict(title='Invalid', kind='bad')]})
