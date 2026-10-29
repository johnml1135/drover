"""Owner tree page, navigation, and batched ledger budget."""
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from drover.ledger import ops, states
from hub.app import create_app
from hub.settings import Settings
from hub.tree import tree_data


class TreeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.groups, self.links, self.inputs = [], [], [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if 'tree-task' in classes:
            self.rows.append(attrs)
        if 'tree-epic' in classes:
            self.groups.append(attrs)
        if tag == 'a':
            self.links.append(attrs.get('href'))
        if tag == 'input':
            self.inputs.append(attrs)


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(Path(self.tmp.name) / 'ledger.db', Settings(
            token='tok', run_watchdog=False, run_dispatcher=False, run_services=False,
            trusted_ui_hosts=('testclient',), tailscale=lambda _: '', ui_allowed_hosts=('testserver',)))
        self.addCleanup(self.app.state.shutdown)
        self.client = TestClient(self.app, follow_redirects=False)
        self.addCleanup(self.client.close)
        self.conn = self.app.state.store.conn
        self.epic = ops.create_epic(self.conn, 'repo', 'First epic')
        self.done = ops.create_epic(self.conn, 'repo', 'Finished epic')
        self.parent = ops.create_task(self.conn, 'Parent title', 'repo', epic_id=self.epic['id'], description='line one\nline two')
        self.child = ops.create_task(self.conn, 'Child title', 'repo', epic_id=self.epic['id'], parent_id=self.parent['id'])
        self.loose = ops.create_task(self.conn, 'Loose title', 'repo')
        self.finished = ops.create_task(self.conn, 'Finished title', 'repo', epic_id=self.done['id'])
        self.conn.execute("UPDATE tasks SET status='landed' WHERE id=?", (self.finished['id'],))
        self.conn.execute("INSERT INTO task_deps(task_id, depends_on, kind) VALUES (?, ?, 'blocks')", (self.child['id'], self.loose['id']))

    def test_grouping_details_search_and_links(self):
        page = self.client.get('/tree', params={'q': 't-G8'})
        self.assertEqual(page.status_code, 200)
        parsed = TreeParser()
        parsed.feed(page.text)
        self.assertEqual(len(parsed.rows), 4)
        self.assertEqual(len(parsed.groups), 3)
        self.assertEqual({r['data-id'] for r in parsed.rows},
                         {self.parent['id'], self.child['id'], self.loose['id'], self.finished['id']})
        self.assertEqual([g['data-id'] for g in parsed.groups], [self.epic['id'], self.done['id'], ''])
        self.assertIn('open', parsed.groups[0])
        self.assertNotIn('open', parsed.groups[1])
        self.assertIn('No epic', page.text)
        self.assertIn('line one\nline two', page.text)
        self.assertIn('⧗ ' + self.loose['id'], page.text)
        self.assertIn('tree-status-proposed', page.text)
        self.assertIn('tree-status-landed', page.text)
        self.assertIn('tree-dim', parsed.rows[2]['class'])
        for task in (self.parent, self.child, self.loose, self.finished):
            self.assertIn('/tasks/' + task['id'], parsed.links)
        self.assertIn('/tree', parsed.links)
        self.assertTrue(any(i.get('id') == 'tree-search' and i.get('value') == 't-G8' for i in parsed.inputs))
        groups = tree_data(self.conn)
        self.assertEqual(groups[0]['tasks'][0]['children'][0]['id'], self.child['id'])
        self.assertEqual(groups[-1]['tasks'][0]['id'], self.loose['id'])
        self.assertEqual(groups[0]['counts']['proposed'], 2)

    def test_jump(self):
        task = ops.create_task(self.conn, 'Mixed case id', 'repo')
        self.conn.execute("UPDATE tasks SET id='T-Xy' WHERE id=?", (task['id'],))
        for q in ('T-Xy', 't-xY'):
            response = self.client.get('/jump', params={'q': q})
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers['location'], '/tasks/T-Xy')
        for q in ('T-g8', 't-NOTFOUND', 'E-1 words'):
            response = self.client.get('/jump', params={'q': q})
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers['location'], '/tree?' + urlencode({'q': q}))

    def test_every_ledger_status_has_light_and_dark_colours(self):
        css = (Path(__file__).resolve().parents[1] / 'hub/static/tree.css').read_text()
        light, dark = css.split('@media(prefers-color-scheme:dark)')
        for status in states.STATES:
            with self.subTest(status=status):
                pattern = r'\.tree-status-' + re.escape(status) + r'(?=[,{])'
                self.assertRegex(light, pattern)
                self.assertRegex(dark, pattern)

    def test_tree_query_and_request_spawn_budget(self):
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            tree_data(self.conn)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(len(statements), 2)
        with patch('drover.placement_git.subprocess.run', side_effect=AssertionError('git spawn')):
            response = self.client.get('/tree')
        self.assertEqual(response.status_code, 200)
        self.assertIn('git;desc="0 spawns"', response.headers['server-timing'])


if __name__ == '__main__':
    unittest.main()
