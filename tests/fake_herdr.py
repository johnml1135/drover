"""A stand-in for the herdr binary, passed to `Herdr` as `run`, so no test reaches a real server.

Pane ids key `agents`, `panes` and `screens`. A screen is text, or a list read one entry at a time with the last one
repeating, for a screen that changes between reads. All three stay mutable during a test. `calls` holds every argv
without the binary, and `kwargs` the keywords of each call. Set `closed` for a stopped server, `hang` for a call that
times out (True for every call, or a set of (group, verb)), and `refuse[(group, verb)] = code` for a refusal from a
running server. `pane run` echoes as a shell does: the line stays on its prompt's row, under a new prompt. `restart`
forgets every name, as a restarted server does, and pane ids survive.
"""
import json
import subprocess

_NOT_FOUND = 'pane_not_found'


class FakeHerdr:
    def __init__(self, agents=None, screens=None, panes=None):
        self.agents = dict(agents or {})
        self.screens = dict(screens or {})
        self.panes = dict(panes or {})
        self.calls = []
        self.kwargs = []
        self.closed = False
        self.hang = False
        self.refuse = {}
        self.workspaces = [{'workspace_id': 'w1', 'label': 'drover'}]
        self._opened = 0

    def restart(self):
        for agent in self.agents.values():
            agent['name'] = None

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv[1:]))
        self.kwargs.append(kwargs)
        if self.hang is True or self.hang and tuple(argv[1:3]) in self.hang:
            raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))
        code, out, err = self._answer(list(argv[1:]))
        return subprocess.CompletedProcess(argv, code, _pipe(out, kwargs), _pipe(err, kwargs))

    def _answer(self, args):
        pair = tuple(args[:2])
        if self.closed:
            return _error('server_not_running')
        if pair in self.refuse:
            return _error(self.refuse[pair])
        handler = {('agent', 'get'): self._agent, ('agent', 'list'): self._agents, ('agent', 'read'): self._read,
                   ('agent', 'rename'): self._rename, ('pane', 'get'): self._pane, ('pane', 'list'): self._panes, ('pane', 'read'): self._read,
                   ('pane', 'close'): self._close, ('pane', 'run'): self._run, ('tab', 'create'): self._tab_create,
                   ('tab', 'list'): self._tabs, ('workspace', 'list'): self._workspaces}.get(pair)
        return handler(args) if handler else _result({})

    def _agent(self, args):
        agent = self.agents.get(args[2])
        return _result({'agent': agent}) if agent else _error('agent_not_found')

    def _rename(self, args):
        agent = self.agents.get(args[2])
        if not agent:
            return _error('agent_not_found')
        agent['name'] = args[3]
        return _result({})

    def _agents(self, args):
        return _result({'agents': list(self.agents.values())})

    def _pane(self, args):
        pane = self.panes.get(args[2])
        return _result({'pane': pane}) if pane else _error(_NOT_FOUND)

    def _panes(self, args):
        return _result({'panes': list(self.panes.values())})

    def _read(self, args):
        screen = self.screens.get(args[2])
        if screen is None:
            return _error(_NOT_FOUND)
        if isinstance(screen, list):
            screen = screen.pop(0) if len(screen) > 1 else screen[0]
        return 0, screen, ''

    def _run(self, args):
        screen = self.screens.get(args[2])
        if isinstance(screen, str) and screen.strip():
            prompt = screen.rstrip('\n').splitlines()[-1]
            self.screens[args[2]] = f"{screen.rstrip(chr(10))}{args[3]}\n{prompt}"
        return _result({})

    def _close(self, args):
        if args[2] not in self.panes:
            return _error(_NOT_FOUND)
        for table in (self.panes, self.agents, self.screens):
            table.pop(args[2], None)
        return _result({})

    def _tab_create(self, args):
        self._opened += 1
        workspace = _option(args, '--workspace', 'w1')
        tab = {'tab_id': f'{workspace}:t{self._opened}', 'workspace_id': workspace}
        pane = {'pane_id': f'{workspace}:p{self._opened}', 'tab_id': tab['tab_id'], 'workspace_id': workspace,
                'cwd': _option(args, '--cwd', ''), 'focused': False, 'terminal_id': f't{self._opened}',
                'scroll': {'offset_from_bottom': 0}}
        self.panes[pane['pane_id']] = pane
        return _result({'tab': tab, 'root_pane': pane})

    def _tabs(self, args):
        return _result({'tabs': [{'tab_id': tab} for tab in sorted({p.get('tab_id') for p in self.panes.values()})]})

    def _workspaces(self, args):
        return _result({'workspaces': self.workspaces})


def _option(args, name, default):
    return args[args.index(name) + 1] if name in args else default


def _result(result):
    return 0, json.dumps({'result': result}), ''


def _error(code):
    return 1, '', json.dumps({'error': {'code': code, 'message': code.replace('_', ' ')}})


def _pipe(text, kwargs):
    """What the caller reads from the pipe: UTF-8 bytes, decoded as the caller asked. Unasked is strict ASCII."""
    data = text if isinstance(text, bytes) else text.encode('utf-8')
    return data.decode(kwargs.get('encoding') or 'ascii', kwargs.get('errors') or 'strict')
