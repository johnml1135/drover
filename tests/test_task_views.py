import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from hub.task_views import RepoStates, derive, markdown, probe
    from tests import test_hub_ui as ui_tests
    from tests.test_hub_ui import SAME
    from tests import test_hub_api as api_tests
    from tests.test_hub_api import AUTH
except ImportError as exc:
    raise unittest.SkipTest(str(exc))

from tests.gitfix import Repo


class StateTests(unittest.TestCase):
    def test_planned_existing_and_missing(self):
        repo = Repo()
        self.addCleanup(repo.cleanup)
        cfg = dict(repo=str(repo.repo), worktrees=str(repo.root / 'wts'))
        t = dict(id='T-a', goal='First paragraph.\n\nSecond.')
        disk = probe(cfg)
        state = derive(t, cfg, disk)
        self.assertEqual((state['branch_state'], state['worktree_state']), ('planned', 'planned'))
        self.assertEqual(state['branch_name'], 'drover/T-a')
        self.assertEqual(state['worktree_name'], str(repo.root / 'wts' / 'drover-T-a'))
        self.assertEqual(state['first_paragraph'], 'First paragraph.')
        repo.git('branch', 'topic')
        t['branch'] = 'topic'
        state = derive(t, cfg, probe(cfg))
        self.assertEqual((state['branch_state'], state['worktree_state']), ('exists', 'planned'))
        dest = repo.root / 'wts' / 'drover-T-a'
        repo.git('worktree', 'add', str(dest), 'topic')
        t['worktree'] = str(dest)
        self.assertEqual(derive(t, cfg, probe(cfg))['worktree_state'], 'exists')
        repo.git('worktree', 'remove', str(dest))
        repo.git('branch', '-D', 'topic')
        state = derive(t, cfg, probe(cfg))
        self.assertEqual((state['branch_state'], state['worktree_state']), ('missing', 'missing'))
        self.assertTrue(derive(t, None)['unknown_repo'])
        self.assertTrue(derive(t, cfg)['state_pending'])
        repo.git('worktree', 'add', '-b', 'drover/T-a', str(dest))
        automatic = derive(dict(id='T-a'), cfg, probe(cfg))
        self.assertEqual((automatic['branch_state'], automatic['worktree_state']), ('exists', 'exists'))

    def test_cache_never_waits_for_git_and_reuses_probe(self):
        entered, release = threading.Event(), threading.Event()
        def slow(prog):
            entered.set()
            release.wait(2)
            return dict(branches={'topic'}, paths=set(), branch_paths={})
        cache = RepoStates(loader=slow)
        self.addCleanup(cache.close)
        cfg = dict(repo='/repo', worktrees='/wts')
        self.assertIsNone(cache.get(cfg))
        self.assertTrue(entered.wait(1))
        self.assertIsNone(cache.get(cfg))
        release.set()
        cache.pending[('/repo', '/wts')].result(timeout=2)
        self.assertEqual(cache.get(cfg)['branches'], {'topic'})
        self.assertIs(cache.get(cfg), cache.get(cfg))

    def test_markdown_escapes_raw_html_and_unsafe_links(self):
        rendered = str(markdown('# Heading\n\n**bold** and `code`\n\n- one\n- two\n\n'
                                '<script>alert(1)</script> [bad](javascript:alert) [broken](https://[) '
                                '[safe](https://example.test)\n\n```\n<img src=x onerror=x>\n```'))
        self.assertIn('<h1>Heading</h1>', rendered)
        self.assertIn('<strong>bold</strong>', rendered)
        self.assertIn('<code>code</code>', rendered)
        self.assertIn('<ul>', rendered)
        self.assertNotIn('<script', rendered)
        self.assertNotIn('<img', rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.assertIn('href="https://example.test"', rendered)


class ContextApiTests(api_tests.HubCase):
    def test_kind_access_description_branch_and_connections(self):
        b = self.task()
        with patch('drover.repos.load_all', return_value={'p': {'default_branch': 'main'}}):
            response = self.post('/tasks', dict(title='Research', repo='p', kind='research',
                                               description='**Why**', branch='topic', read_access=['p@main'],
                                               deps=[dict(depends_on=b, kind='waits-for')], related=[b]))
        self.assertEqual(response.status_code, 201, response.text)
        task = response.json()
        self.assertFalse(task['write'])
        self.assertEqual(task['goal'], '**Why**')
        self.assertEqual(task['description'], '**Why**')
        self.assertEqual(task['branch'], 'topic')
        tid = task['id']
        other = self.get('/tasks/' + b).json()
        self.assertIn(tid, other['related'])
        self.assertIn(tid, other['dependents'])
        r = self.c.patch('/api/v1/tasks/' + tid, headers=AUTH, json={'kind': 'invalid'})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.post('/tasks', {'title': 'Bad', 'kind': 'invalid'}).status_code, 422)
        self.assertEqual(self.post('/tasks', {'title': 'Bad', 'read_access': ['missing']}).status_code, 422)
        r = self.c.patch('/api/v1/tasks/' + b, headers=AUTH, json={'deps': [tid]})
        self.assertEqual(r.status_code, 400)
        r = self.c.patch('/api/v1/tasks/' + tid, headers=AUTH, json={'goal': 'Old client', 'deps': [], 'related': []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.get('/tasks/' + tid).json()['description'], 'Old client')
        self.assertEqual(self.get('/tasks/' + tid).json()['related'], [])
        self.assertEqual(self.post('/events', dict(kind='dispatch.access', data={})).status_code, 403)


class ContextUiTests(ui_tests.UiTests):
    def test_accept_findings_completes_read_work_with_existing_owner_guards(self):
        tid = self.st.create_task('Findings', kind='research')['id']
        self.st.approve(tid)
        self.st.set_status(tid, 'claimed', 'dispatcher')
        rid = self.st.start_run(tid, 'worker', 'codex')['id']
        self.st.finish_run(rid, 'read-only-done', note='Findings ready')
        self.st.set_status(tid, 'needs-owner', 'dispatcher', 'Review findings')
        for path in ('/inbox', '/tasks/' + tid):
            self.assertIn('Accept findings', self.c.get(path).text)
        action = f'/items/task/{tid}/approve'
        for headers in ({}, {'Sec-Fetch-Site': 'cross-site'}, dict(SAME, Host='untrusted.example')):
            self.assertEqual(self.c.post(action, headers=headers).status_code, 403)
            self.assertEqual(self.st.get_task(tid)['status'], 'needs-owner')
        self.assertEqual(self.c.post(action, headers=SAME).status_code, 200)
        self.assertEqual(self.st.get_task(tid)['status'], 'landed')
        self.assertEqual(len(self.st.list_runs(tid)), 1)

    def test_task_page_approve_and_reject(self):
        for status in ('proposed', 'draft', 'needs-owner'):
            for decision, expected in (('approve', 'ready'), ('reject', 'cancelled')):
                with self.subTest(status=status, decision=decision):
                    tid = self.st.create_task('Awaiting decision')['id']
                    if status == 'needs-owner':
                        self.st.approve(tid)
                    if status != 'proposed':
                        self.st.set_status(tid, status, 'owner')
                    page = self.c.get('/tasks/' + tid)
                    self.assertEqual(page.status_code, 200, page.text)
                    action = f'/items/task/{tid}/{decision}'
                    self.assertIn(f'action="{action}"', page.text)
                    self.assertIn(f'hx-post="{action}"', page.text)
                    response = self.c.post(action, headers=SAME)
                    self.assertEqual(response.status_code, 200, response.text)
                    task = self.st.get_task(tid)
                    self.assertEqual(task['status'], expected)
                    if decision == 'approve' and status in ('proposed', 'draft'):
                        self.assertIsNotNone(task['approved_at'])

    def test_task_page_decisions_require_owner_and_same_origin(self):
        for decision in ('approve', 'reject'):
            for headers in ({}, {'Sec-Fetch-Site': 'cross-site'},
                            dict(SAME, Host='untrusted.example')):
                with self.subTest(decision=decision, headers=headers):
                    tid = self.st.create_task('Awaiting decision')['id']
                    response = self.c.post(f'/items/task/{tid}/{decision}', headers=headers)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(self.st.get_task(tid)['status'], 'proposed')
                    self.assertIsNone(self.st.get_task(tid)['approved_at'])

    def test_full_page_card_partial_connections_and_owner_edit(self):
        a = self.st.create_task('Task context', description='# Context\n\nFirst paragraph.\n\n<script>bad</script>',
                                kind='review', acceptance=['Read it'], scope_files=['docs'])['id']
        b = self.st.create_task('Dependent', deps=[a], related=[a])['id']
        rid = self.st.start_run(a, 'worker', 'codex')['id']
        self.st.finish_run(rid, 'read-only-done', note='Findings')
        page = self.c.get('/tasks/' + a)
        self.assertEqual(page.status_code, 200, page.text)
        for text in ('<h1>Context</h1>', 'read-only', 'unknown repo', 'Read access', 'Read it',
                     'Depends on this', 'Related', b, 'read-only-done', 'Events', 'Scope'):
            self.assertIn(text, page.text)
        self.assertNotIn('<script>bad</script>', page.text)
        partial = self.c.get('/tasks/' + a + '?view=card', headers={'HX-Request': 'true'})
        self.assertNotIn('<!doctype', partial.text)
        self.assertIn('task-card', partial.text)
        for path in ('/inbox', '/plan'):
            text = self.c.get(path).text
            self.assertIn('task-card', text)
            self.assertIn('/tasks/' + a, text)
        self.assertEqual(self.c.post('/tasks/' + a + '/edit', data={'title': 'New'}).status_code, 403)
        self.assertEqual(self.c.post('/tasks/' + a + '/edit', data={'title': 'New'}, headers=SAME).status_code, 200)
        self.assertEqual(self.st.get_task(a)['title'], 'New')
        self.assertEqual(self.c.post('/tasks/' + a + '/edit', data={'kind': 'bad'}, headers=SAME).status_code, 422)
        self.assertIn('Connected tasks: none', self.c.get('/tasks/' + self.st.create_task('Alone')['id']).text)

    def test_unknown_task_is_404(self):
        self.assertEqual(self.c.get('/tasks/T-missing').status_code, 404)

    def test_import_rejects_invalid_connections_without_partial_rows(self):
        response = self.st.import_tasks([dict(title='Broken', epic='New epic', related=['T-missing']),
                                         dict(title='Good', epic='New epic', kind='research')], dry_run=False)
        self.assertEqual(response['counts']['invalid'], 1)
        self.assertEqual([t['title'] for t in self.st.list_tasks()], ['Good'])
        self.assertEqual(len(self.st.list_epics()), 1)
        self.assertFalse(self.st.list_tasks()[0]['write'])
