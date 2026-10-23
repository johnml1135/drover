"""HTTP-only client wire contract, including credential and redirect boundaries."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drover.notifications.__main__ import main


class NotificationCliTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import MagicMock
        from urllib.error import HTTPError
        self.requests = []
        self.status = 200
        self.response = {'ok': True}
        self.url = 'http://127.0.0.1:8766'
        response = MagicMock()
        response.__enter__.return_value = response

        def open_request(request, timeout):
            data = request.data
            self.requests.append((request.method, request.full_url.removeprefix(self.url), request.get_header('Authorization'), json.loads(data) if data else None))
            if self.status != 200:
                raise HTTPError(request.full_url, self.status, 'private-token', {}, io.BytesIO(b'private-token'))
            response.read.return_value = json.dumps(self.response).encode()
            return response

        opener = MagicMock()
        opener.open.side_effect = open_request
        self.opener_patch = patch('drover.notifications.__main__.build_opener', return_value=opener)
        self.opener_patch.start()
        self.addCleanup(self.opener_patch.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {'DIRECTOR_TEST_TOKEN': 'private-token'}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--url', self.url, '--credential-env', 'DIRECTOR_TEST_TOKEN', *args])
        return code, out.getvalue(), err.getvalue()

    def test_follow_tune_and_commit_echo_observed_versions(self):
        self.assertEqual(self.run_cli('follow', '{"selectors":{"tasks":["T1"]}}')[0], 0)
        self.assertEqual(self.requests[-1], ('POST', '/api/v1/notifications/follow', 'Bearer private-token', {'selectors': {'tasks': ['T1']}}))
        self.assertEqual(self.run_cli('tune', 'S1', '{"expected_revision":1,"preferences":{"mode":"muted"}}')[0], 0)
        self.assertEqual(self.requests[-1][0:2], ('PATCH', '/api/v1/notifications/subscriptions/S1'))
        body = {'lease_token': 'lease', 'checkpoint': {'step': 2}, 'delivery_ids': ['notification:7'], 'delivery_versions': {'notification:7': 12}, 'request_key': 'commit-1', 'release': True}
        code, out, err = self.run_cli('commit', json.dumps(body))
        self.assertEqual((code, json.loads(out), err), (0, {'ok': True}, ''))
        self.assertEqual(self.requests[-1][3], body)

    def test_session_flow_groups_inbox_and_control_application(self):
        for args, method, path in [
            (('lease', '{"ttl":30}'), 'POST', '/lease'),
            (('inbox', '--wait', '1', '--limit', '4'), 'GET', '/inbox?wait=1.0&limit=4'),
            (('group', 'create', 'research', '{"parent_id":"T1"}'), 'POST', '/groups'),
            (('group', 'seal', 'research', '{"expected_revision":2,"request_key":"seal"}'), 'POST', '/groups/research/seal'),
            (('control', 'state'), 'GET', '/controls'),
            (('control', 'apply', 'C1', '{"lease_token":"lease","digest":"sha"}'), 'POST', '/controls/C1/apply')]:
            self.assertEqual(self.run_cli(*args)[0], 0)
            self.assertEqual(self.requests[-1][0:2], (method, '/api/v1/notifications' + path))

    def test_redirect_and_remote_error_do_not_expose_credentials(self):
        for status in (302, 403):
            self.status, self.response = status, {'detail': 'private-token secret payload'}
            count = len(self.requests)
            code, out, err = self.run_cli('health')
            self.assertEqual(code, 2)
            self.assertEqual(len(self.requests), count + 1)
            self.assertEqual(out, '')
            self.assertNotIn('private-token', err)
            self.assertNotIn('secret', err)
            self.assertIn(str(status), err)

    def test_private_credential_file_and_unreadable_error_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private-token'
            path.write_text('private-token')
            if os.name != 'nt':
                path.chmod(0o644)
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = main(['--url', self.url, '--credential-file', str(path), 'health'])
                self.assertEqual(code, 2)
                self.assertFalse(self.requests)
                self.assertNotIn('private-token', err.getvalue())
            path.chmod(0o600)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--url', self.url, '--credential-file', str(path), 'health']), 0)

    def test_redirect_handler_refuses_remote_location(self):
        from drover.notifications.__main__ import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://other.test/'))
