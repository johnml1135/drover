"""CI repository observations and a scripted execution adapter; no child processes."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from drover import ci, placement, runner


class Handle:
    pid = 1

    def __init__(self, launcher, spec):
        self.launcher, self.spec = launcher, spec
        self.result = None

    def poll(self):
        if self.result is not None:
            return self.result
        if self.launcher.pending and not self.launcher.released:
            return None
        self.result = self.launcher.finish(self.spec)
        return self.result

    def cancel(self):
        pass


class Launcher:
    """Run real CI._execute with scripted command results; preserve status/adoption files."""
    def __init__(self, runs):
        self.runs, self.specs, self.handles = runs, [], {}
        self.commands = []
        self.code, self.output, self.after_command = 0, '', lambda: None
        self.pending = self.released = False
        self.on_start = lambda: None
        self.execution = None
        self.outcomes = {}
        self.wrap_command = lambda argv, priority: list(argv)

    def launch(self, spec):
        self.specs.append(spec)
        self.on_start()
        handle = self.handles[spec['run_id']] = Handle(self, spec)
        runner.write_json_atomic(runner.run_dir(self.runs, spec['run_id']) / runner.STATUS_FILE,
            dict(state='running', run_id=spec['run_id'], nonce=spec['nonce'], runner_pid=1))
        return handle

    def adopt(self, spec):
        return self.handles[spec['run_id']]

    def finish(self, spec):
        directory = runner.run_dir(self.runs, spec['run_id'])
        plan = json.loads((directory / 'plan.json').read_text())
        # A wsl plan carries /mnt paths and runs inside WSL; this host runs it in-process, so use native spellings.
        plan.update({k: placement.native(plan[k]) for k in ('repo', 'out', 'log')})
        def run(argv, **kw):
            self.commands.append(argv)
            with (directory / runner.OUT_FILE).open('a') as output:
                output.write(self.output)
            self.after_command()
            return subprocess.CompletedProcess(argv, self.outcomes.get(spec['placement'], self.code))
        with patch.object(ci, 'subprocess', SimpleNamespace(run=run)), \
                patch.object(placement, 'ci_command', side_effect=self.wrap_command):
            try:
                code = ci._execute(plan)
            except (ci.CIError, OSError, ValueError, KeyError) as error:
                (directory / runner.OUT_FILE).write_text(f'CI: {error}')
                code = 2
        execution = self.execution or dict(status='done' if code == 0 else 'error', returncode=code)
        runner.write_json_atomic(directory / runner.STATUS_FILE,
            dict(state='exited', exit_code=execution['returncode'], nonce=spec['nonce'], run_id=spec['run_id']))
        return {**execution, 'record': None, 'text': '', 'handoff': None, 'timeout': execution.get('timeout'),
                'usage': dict(tokens_in=0, tokens_out=0, tokens_cached=0, context=0)}

    def runners(self, want_wsl=False):
        return {rid: h.spec['placement'] for rid, h in self.handles.items() if h.result is None}
