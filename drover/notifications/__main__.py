"""HTTP-only director client. Never opens a ledger or follows a redirect."""
import argparse
import json
import os
import stat
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ClientError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, url, credential):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ClientError('a plain HTTP(S) hub URL is required')
        if not credential or any(ch.isspace() for ch in credential):
            raise ClientError('invalid credential')
        self.url = url.rstrip('/') + '/api/v1/notifications'
        self.credential = credential
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        req = Request(self.url + path, data=data, method=method, headers={
            'Authorization': 'Bearer ' + self.credential, 'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with self.opener.open(req, timeout=40) as response:
                return json.loads(response.read(2 * 1024 * 1024))
        except HTTPError as error:
            error.close()
            # Remote messages, URLs and redirect locations may contain credentials.
            raise ClientError(f'HTTP {error.code}: notification request refused') from None
        except (URLError, OSError, ValueError):
            raise ClientError('notification request failed') from None


def credential(args):
    if args.credential_env:
        value = os.environ.get(args.credential_env, '')
    else:
        path = Path(args.credential_file)
        if path.is_symlink() or not path.is_file():
            raise ClientError('credential file must be a private regular file')
        if os.name != 'nt' and (path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO) or path.stat().st_uid != os.getuid()):
            raise ClientError('credential file must be owned by you with mode 600')
        value = path.read_text(encoding='utf-8').strip()
        if value.startswith('{'):
            try:
                value = json.loads(value)['credential']
            except (ValueError, KeyError, TypeError):
                raise ClientError('credential file must contain a token or session JSON') from None
    if not isinstance(value, str) or not value:
        raise ClientError('credential reference is empty')
    return value


def payload(value):
    try:
        result = json.load(sys.stdin) if value == '-' else json.loads(Path(value[1:]).read_text(encoding='utf-8') if value.startswith('@') else value)
    except (ValueError, OSError):
        raise ClientError('payload must be JSON, @file, or - for stdin') from None
    if not isinstance(result, dict):
        raise ClientError('payload must be a JSON object')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('DROVER_HUB_URL', 'http://127.0.0.1:8766'))
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument('--credential-file')
    sources.add_argument('--credential-env', metavar='VARIABLE_NAME')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('health', 'checkpoint'):
        commands.add_parser(name)
    for name in ('follow', 'submit', 'commit', 'lease', 'broadcast'):
        cmd = commands.add_parser(name)
        cmd.add_argument('payload', nargs='?', default='{}')
    tune = commands.add_parser('tune'); tune.add_argument('subscription_id'); tune.add_argument('payload')
    inbox = commands.add_parser('inbox'); inbox.add_argument('--wait', type=float, default=0); inbox.add_argument('--limit', type=int, default=50)
    replay = commands.add_parser('replay'); replay.add_argument('--after'); replay.add_argument('--limit', type=int, default=50)
    group = commands.add_parser('group'); group.add_argument('operation', choices=('create', 'get', 'enroll', 'seal')); group.add_argument('group_id'); group.add_argument('payload', nargs='?', default='{}')
    control = commands.add_parser('control'); control.add_argument('operation', choices=('state', 'apply')); control.add_argument('control_id', nargs='?'); control.add_argument('payload', nargs='?', default='{}')
    issue = commands.add_parser('issue'); issue.add_argument('operation', choices=('claim', 'hold')); issue.add_argument('issue_id'); issue.add_argument('payload')
    args = parser.parse_args(argv)
    try:
        client = Client(args.url, credential(args))
        method, path, body = 'GET', '/' + args.command, None
        if args.command in ('follow', 'submit', 'commit', 'lease', 'broadcast'):
            method, body = 'POST', payload(args.payload)
            if args.command == 'submit':
                path = '/tasks'
        elif args.command == 'tune':
            method, path, body = 'PATCH', '/subscriptions/' + quote(args.subscription_id, safe=''), payload(args.payload)
        elif args.command == 'inbox':
            path += f'?wait={args.wait}&limit={args.limit}'
        elif args.command == 'replay':
            path += f'?limit={args.limit}' + ('&after=' + quote(args.after, safe='') if args.after else '')
        elif args.command == 'group':
            path = '/groups' if args.operation == 'create' else '/groups/' + quote(args.group_id, safe='')
            if args.operation != 'get':
                method, body = 'POST', payload(args.payload)
                if args.operation == 'create':
                    body['group_id'] = args.group_id
                else:
                    path += '/' + args.operation
        elif args.command == 'control':
            path = '/controls'
            if args.operation == 'apply':
                if not args.control_id:
                    raise ClientError('control apply requires a control identity')
                method, path, body = 'POST', path + '/' + quote(args.control_id, safe='') + '/apply', payload(args.payload)
        elif args.command == 'issue':
            method, path, body = 'POST', '/issues/' + quote(args.issue_id, safe='') + '/' + args.operation, payload(args.payload)
        result = client.request(method, path, body)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ClientError, OSError, ValueError):
        # Credentials and payloads never appear in diagnostics, even remote errors.
        message = sys.exc_info()[1]
        print(json.dumps({'error': str(message) if isinstance(message, ClientError) else 'notification request failed'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
