"""The ledger's record of agents in panes and of what Drover wrote to them."""
import sqlite3
from pathlib import Path

from drover import pane_screen
from drover.ledger import db as ledger_db, ops_panes
from drover.ledger.schema import MIGRATIONS
from tests.test_ledger import Base


def fields(**over):
    return {'pane_id': 'w1:p1', 'kind': 'claude', 'seat': 'claude-main', 'placement': 'windows', 'started_by': 'drover',
            **over}


class Agents(Base):
    def test_record_keeps_every_field_and_starts_in_starting(self):
        row = ops_panes.record_agent(self.conn, **fields(role_id='director', run_id=4, terminal_id='t1', name='dir',
                                                         session_id='s1', cwd='H:/repos/x'))
        self.assertEqual((row['role_id'], row['run_id'], row['terminal_id'], row['name'], row['session_id'], row['cwd']),
                         ('director', 4, 't1', 'dir', 's1', 'H:/repos/x'))
        self.assertEqual((row['seat'], row['placement'], row['kind'], row['started_by']),
                         ('claude-main', 'windows', 'claude', 'drover'))
        self.assertEqual(row['state'], 'starting')
        self.assertEqual(row['created'], row['updated'])
        self.assertRegex(row['created'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_seat_and_placement_are_required(self):
        for name in ('seat', 'placement', 'pane_id', 'kind', 'started_by'):
            for bad in (None, ''):
                with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                    ops_panes.record_agent(self.conn, **fields(**{name: bad}))
            missing = fields()
            del missing[name]
            with self.subTest(name=name), self.assertRaises(ValueError):
                ops_panes.record_agent(self.conn, **missing)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM pane_agents').fetchone()[0], 0)

    def test_unknown_values_are_refused(self):
        for over in ({'placement': 'side'}, {'kind': 'gemini'}, {'started_by': 'someone'}, {'state': 'unknown'},
                     {'id': 9}, {'created': 'x'}, {'colour': 'red'}):
            with self.subTest(over), self.assertRaises(ValueError):
                ops_panes.record_agent(self.conn, **fields(**over))

    def test_update_changes_the_row_and_its_time(self):
        row = ops_panes.record_agent(self.conn, **fields())
        self.conn.execute("UPDATE pane_agents SET updated='2020-01-01T00:00:00Z' WHERE id=?", (row['id'],))
        new = ops_panes.update_agent(self.conn, row['id'], state='live', session_id='s2', context_tokens=1200,
                                     handoff_state='requested')
        self.assertEqual((new['state'], new['session_id'], new['context_tokens'], new['handoff_state']),
                         ('live', 's2', 1200, 'requested'))
        self.assertNotEqual(new['updated'], '2020-01-01T00:00:00Z')
        self.assertEqual(new['created'], row['created'])

    def test_what_identifies_an_agent_never_changes(self):
        row = ops_panes.record_agent(self.conn, **fields())
        for over in ({'seat': 'other'}, {'placement': 'wsl'}, {'kind': 'codex'}, {'started_by': 'owner'},
                     {'pane_id': 'w1:p2'}, {'created': 'x'}, {'id': 2}, {'colour': 'red'}, {'state': 'unknown'}):
            with self.subTest(over), self.assertRaises(ValueError):
                ops_panes.update_agent(self.conn, row['id'], **over)

    def test_a_gone_agent_stays_gone(self):
        row = ops_panes.record_agent(self.conn, **fields())
        ops_panes.update_agent(self.conn, row['id'], state='gone')
        with self.assertRaises(ValueError):
            ops_panes.update_agent(self.conn, row['id'], state='live')
        self.assertEqual(ops_panes.update_agent(self.conn, row['id'], state='gone', name='x')['name'], 'x')

    def test_update_of_a_missing_row_is_not_found(self):
        with self.assertRaises(LookupError):
            ops_panes.update_agent(self.conn, 99, state='live')

    def test_open_agents_are_those_whose_pane_may_still_exist(self):
        rows = {state: ops_panes.record_agent(self.conn, **fields(pane_id=f'w1:p{i}', state=state))
                for i, state in enumerate(ops_panes.AGENT_STATES)}
        wsl = ops_panes.record_agent(self.conn, **fields(pane_id='w2:p1', placement='wsl', kind='codex', state='live'))
        self.assertEqual([r['id'] for r in ops_panes.open_agents(self.conn)],
                         sorted([r['id'] for s, r in rows.items() if s != 'gone'] + [wsl['id']]))
        self.assertEqual([r['id'] for r in ops_panes.open_agents(self.conn, placement='wsl')], [wsl['id']])

    def test_by_pane_finds_the_newest_row_and_never_a_gone_one(self):
        self.assertIsNone(ops_panes.by_pane(self.conn, 'w1:p1'))
        first = ops_panes.record_agent(self.conn, **fields())
        self.assertEqual(ops_panes.by_pane(self.conn, 'w1:p1')['id'], first['id'])
        ops_panes.update_agent(self.conn, first['id'], state='gone')
        self.assertIsNone(ops_panes.by_pane(self.conn, 'w1:p1'))
        second = ops_panes.record_agent(self.conn, **fields(state='live'))
        third = ops_panes.record_agent(self.conn, **fields(state='retired'))
        self.assertEqual(ops_panes.by_pane(self.conn, 'w1:p1')['id'], third['id'])
        self.assertGreater(third['id'], second['id'])


class Writes(Base):
    def setUp(self):
        super().setUp()
        self.agent = ops_panes.record_agent(self.conn, **fields())

    def test_write_stores_a_digest_and_a_short_head_not_the_text(self):
        text = 'Read your inbox and commit a checkpoint. ' * 4 + 'the private tail'
        row = ops_panes.note_write(self.conn, self.agent['id'], 'wake', text)
        self.assertEqual(row['head'], ' '.join(text.split())[:60])
        self.assertEqual(len(row['digest']), 64)
        self.assertEqual((row['agent_id'], row['kind'], row['state'], row['reason']),
                         (self.agent['id'], 'wake', 'pending', None))
        dump = '\n'.join(self.conn.iterdump())
        self.assertNotIn('the private tail', dump)

    def test_digest_is_the_one_the_composer_is_compared_with(self):
        text = '  Line one\n\n   line   two\twith caf\u00e9  \n'
        row = ops_panes.note_write(self.conn, self.agent['id'], 'brief', text)
        self.assertEqual((row['digest'], row['head']), pane_screen.prompt_fingerprint(text))
        self.assertTrue(pane_screen.own_prompt('Line one line two with caf\u00e9', row['digest'], None, 0))

    def test_a_write_needs_an_agent(self):
        with self.assertRaises(LookupError):
            ops_panes.note_write(self.conn, 99, 'wake', 'x')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM pane_writes').fetchone()[0], 0)

    def test_finish_sets_state_and_reason(self):
        row = ops_panes.note_write(self.conn, self.agent['id'], 'wake', 'x')
        done = ops_panes.finish_write(self.conn, row['id'], 'unsent', 'dialog')
        self.assertEqual((done['state'], done['reason'], done['digest']), ('unsent', 'dialog', row['digest']))
        self.assertIsNone(ops_panes.finish_write(self.conn, row['id'], 'sent')['reason'])
        with self.assertRaises(LookupError):
            ops_panes.finish_write(self.conn, 99, 'sent')
        with self.assertRaises(ValueError):
            ops_panes.finish_write(self.conn, row['id'], '')

    def test_last_write_is_the_newest_of_its_kind(self):
        other = ops_panes.record_agent(self.conn, **fields(pane_id='w1:p2'))
        self.assertIsNone(ops_panes.last_write(self.conn, self.agent['id']))
        wake = ops_panes.note_write(self.conn, self.agent['id'], 'wake', 'a')
        brief = ops_panes.note_write(self.conn, self.agent['id'], 'brief', 'b')
        ops_panes.note_write(self.conn, other['id'], 'wake', 'c')
        self.assertEqual(ops_panes.last_write(self.conn, self.agent['id'])['id'], brief['id'])
        self.assertEqual(ops_panes.last_write(self.conn, self.agent['id'], 'wake')['id'], wake['id'])
        self.assertIsNone(ops_panes.last_write(self.conn, self.agent['id'], 'stop'))


class Migration(Base):
    def test_v19_applies_to_a_copy_of_a_v18_ledger(self):
        path = Path(self.tmp.name) / 'v18.db'
        old = ledger_db.connect(path, migrate_db=False)
        self.conns.append(old)
        self.assertEqual(ledger_db.migrate(old, MIGRATIONS[:18]), 18)
        old.execute("INSERT INTO settings(key,value) VALUES('kept','yes')")
        old.commit()
        copy_path = Path(self.tmp.name) / 'v18-copy.db'
        copy = ledger_db.connect(copy_path, migrate_db=False)
        self.conns.append(copy)
        old.backup(copy)
        self.assertEqual(ledger_db.migrate(copy), MIGRATIONS[-1][0])
        self.assertGreaterEqual(MIGRATIONS[-1][0], 19)
        self.assertEqual(copy.execute("SELECT value FROM settings WHERE key='kept'").fetchone()[0], 'yes')
        self.assertEqual(ledger_db.migrate(copy), MIGRATIONS[-1][0])
        self.assertEqual(ledger_db.schema_version(old), 18)
        self.assertIsNone(old.execute("SELECT 1 FROM sqlite_master WHERE name='pane_agents'").fetchone())
        agent = ops_panes.record_agent(copy, **fields())
        self.assertEqual(ops_panes.note_write(copy, agent['id'], 'wake', 'x')['agent_id'], agent['id'])

    def test_the_schema_itself_refuses_a_row_without_seat_or_placement(self):
        insert = ("INSERT INTO pane_agents(pane_id,kind,seat,placement,started_by,state,created,updated) "
                  "VALUES('p','claude',?,?,'drover','live','t','t')")
        for params in ((None, 'windows'), ('s', None), ('s', 'side')):
            with self.subTest(params), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(insert, params)
