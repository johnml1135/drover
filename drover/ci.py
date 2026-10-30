"""Checks and full CI: repo scripts on exact commits, supervised by the runner.
CI.prepare produces a runner spec for asynchronous checks. CI.run starts or adopts
full CI and waits for its result. Planning, docs classification, output and verdicts
are shared by both. Targeted diagnostics require the repository script contract.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from drover import placement, repos, runner, contract
    from drover.ledger import ci as slots, ops
    from drover.ci_common import (VERSION, now, _norm, _glob_re, DOCS_EXTENSIONS, DOCS_EXACT,
                                  _glob_hit, docs_only, status_problems, validate_receipt, evaluate_result, INFRA_RETRIES, read_result, _tail)
else:
    from . import placement, repos, runner, contract
    from .ledger import ci as slots, ops
    from .ci_common import (VERSION, now, _norm, _glob_re, DOCS_EXTENSIONS, DOCS_EXACT,
                            _glob_hit, docs_only, status_problems, validate_receipt, evaluate_result, INFRA_RETRIES, read_result, _tail)
GIT_PROMPT_ENV = {'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never', 'GIT_ASKPASS': ''}
class CIError(RuntimeError):
    pass
class CIInfrastructure(CIError):
    """A terminal infrastructure failure, carrying its structured CI outcome."""
    def __init__(self, detail):
        self.detail = detail
        super().__init__(detail.get('reason') or '\n'.join(detail.get('log_tail', [])))
class CIInterrupted(CIError):
    """Supervision failed before termination was confirmed; resume before changing the checkout."""
    def __init__(self, message, detail=None):
        self.detail = {'outcome': 'deferred' if type(self).__name__ == 'CIDeferred' else 'termination-unknown',
                       'reason': str(message), 'failures': [], **(detail or {})}
        super().__init__(message)
class CIDeferred(CIInterrupted):
    """No execution issued: retain the prepared request until resources recover."""
def git_environment(base=None):
    """Environment that keeps Git noninteractive even when a machine has an interactive credential helper."""
    env = os.environ.copy() if base is None else dict(base)
    env.update(GIT_PROMPT_ENV)
    return env
# ---- git ------------------------------------------------------------------------------------------------
def git(repo, *args, check=True):
    try:
        result = placement.git(repo, *args, env=git_environment(), timeout=60)
    except subprocess.TimeoutExpired as error:
        raise CIError(f'git {" ".join(args)} timed out after 60 seconds') from error
    if check and result.returncode:
        raise CIError(f'git {" ".join(args)}: {result.stderr.strip()}')
    return result
def head(repo):
    return git(repo, 'rev-parse', 'HEAD').stdout.strip()
def dirty(repo, *, post_run=False):
    """Tracked edits, plus bounded untracked discovery before testing or verifying a commit."""
    tracked = git(repo, *placement.status_args(repo)).stdout.strip()
    if tracked or post_run:
        return tracked
    return git(repo, 'ls-files', '--others', '--exclude-standard', '--directory', '--no-empty-directory').stdout.strip()
def contains(repo, base_ref, sha):
    base = git(repo, 'rev-parse', '--verify', '--quiet', base_ref + '^{commit}', check=False).stdout.strip()
    if not base:
        return None, False
    return base, git(repo, 'merge-base', '--is-ancestor', base, sha, check=False).returncode == 0
# ---- changed paths and documentation guards ---------------------------------------------------------------
def changed_paths(repo, base, tip='HEAD'):
    return [p for p in git(repo, '-c', 'core.quotepath=false', 'diff', '--name-only', '--no-renames', '-z', f'{base}...{tip}').stdout.split('\0') if p]
def changed_docs_only(repo, base, tip, globs):
    """docs_only for `base...tip`, and false when any changed path is or was a symlink (git mode 120000)."""
    paths = changed_paths(repo, base, tip)
    if not docs_only(paths, globs):
        return False
    raw = git(repo, 'diff', '--raw', '--no-renames', '-z', f'{base}...{tip}').stdout
    return not any(f.startswith(':') and '120000' in f.lstrip(':').split()[:2] for f in raw.split('\0'))


def problems(receipt, repo, base_ref):
    """Why `receipt` does not license landing the repo's HEAD ([] = it does)."""
    found = status_problems(receipt)
    current = head(repo)
    if receipt.get('sha') != current:
        found.append(f'receipt is for {str(receipt.get("sha"))[:12]}, HEAD is {current[:12]}')
    if dirty(repo):
        found.append('working tree has uncommitted changes')
    base, has_base = contains(repo, base_ref, current)
    if base is None:
        found.append(f'base {base_ref} not found; fetch it first')
    elif not has_base:
        found.append(f'HEAD does not contain {base_ref} {base[:12]}; rebase and regate')
    return found


class CI:
    def __init__(self, runs_dir, launcher=None, poll_s=0.25, resource_check=None, *, source=None):
        from .launch import RunnerLauncher
        self.source = source
        self.runs_dir = str(runs_dir)
        self.launcher = launcher or RunnerLauncher(runs_dir=self.runs_dir)
        self.poll_s = poll_s       # how often run() polls a runner and a slot queue; tests inject a short one
        self.resource_check = resource_check  # admission probe; None means resources.check, tests inject one
        self._resumed = {}

    def prepare(self, prog, checkout, commit, run_id, *, kind='checks', platform=None, base=None, priority='P2', force_full=False, module=None, tier=None):
        """Plan a repo script at commit and return its runner spec and CI context."""
        source = self.source
        (source.prepare_worktree if source else placement.prepare_worktree)(checkout)
        prog = (source.load if source else repos.load)(prog, commit, checkout)
        policy = repos.ci_policy(prog)
        platform = platform or repos.gate_platforms(prog)[0]
        base = base or policy['base']
        paths = (source.changed_paths if source else changed_paths)(checkout, base, commit)
        docs = not (force_full or module) and policy['docs_only'] and (source.changed_docs_only if source else changed_docs_only)(
            checkout, base, commit, policy['docs_only_globs'])
        effective = 'docs' if docs and kind == 'checks' else kind
        commands = []
        if docs:
            commands.append(['git', '-c', 'core.whitespace=-blank-at-eol', 'diff', '--check', f'{base}...{commit}'])
        if not docs or kind == 'full' or repos.has_script(prog, 'docs'):
            commands.append(repos.script_argv(prog, 'docs' if docs and kind == 'checks' else 'test', platform))
        cfg = repos.run_config(prog)
        if module:
            script = Path(placement.native(checkout)) / prog['scripts']['test']
            try:
                supported = 'DROVER_CI_DIAGNOSTIC_V1' in script.read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                supported = False
            if not re.fullmatch(r'tests\.test_[A-Za-z0-9_]+', module) or tier not in ('default', 'integration') or not supported:
                raise CIInfrastructure(dict(reason='base test script lacks a valid module diagnostic selector', platform=platform))
        cfg['env'] = {**cfg['env'], **GIT_PROMPT_ENV, 'DROVER_CI_MODULE': module or '', 'DROVER_CI_TIER': tier or '',
                      'DROVER_CI_KIND': effective,
                      'DROVER_CHANGED_FILES': json.dumps(paths)}
        d = runner.run_dir(self.runs_dir, run_id)
        d.mkdir(parents=True, exist_ok=True)
        result_path = d / 'receipt.json'
        nonce = uuid.uuid4().hex
        plan = {'run_id': run_id, 'nonce': nonce, 'repo': placement.ci_path(platform, checkout), 'sha': commit, 'base': base,
                'kind': kind, 'platform': platform, 'commands': commands, 'priority': priority,
                'out': placement.ci_path(platform, result_path),
                'log': placement.ci_path(platform, d / runner.OUT_FILE)}
        runner.write_json_atomic(d / 'plan.json', plan)
        spec = {'run_id': run_id, 'role': 'gate', 'backend': 'local', 'model': 'gate', 'effort': None,
                'argv': placement.ci_invocation(platform, Path(__file__), d / 'plan.json'),
                'cwd': str(checkout), 'placement': platform, 'nonce': nonce,
                'wall_s': policy['timeout_s'], 'idle_s': policy['idle_s'], 'user_prompt': '', 'run_config': cfg}
        return {'spec': spec, 'receipt': str(result_path), 'kind': kind, 'sha': commit,
                'files': paths, 'scope': None, 'diff_lines': (source.diff_lines if source else self._diff_lines)(checkout, base, commit)}

    @staticmethod
    def _diff_lines(checkout, base, commit):
        rows = git(checkout, 'diff', '--numstat', '--no-renames', f'{base}...{commit}').stdout.splitlines()
        return sum(int(n) for row in rows for n in row.split('\t')[:2] if n.isdigit())

    def result(self, run_id, execution=None, spec=None):
        """Return the CI result and failure classification after a runner has finished."""
        d = runner.run_dir(self.runs_dir, run_id)
        plan = read_result(d / 'plan.json')
        receipt = read_result(d / 'receipt.json')
        status = runner.read_status(self.runs_dir, run_id) or {}
        execution = execution or {'status': status.get('state'), 'returncode': status.get('exit_code')}
        return evaluate_result(receipt, plan, status, execution,
                               _tail(d / runner.OUT_FILE, contract.EXCERPT_LINES - 1))

    def stop_active(self, conn, epoch):
        """Stop saved group CI by nonce, including a hub restart during STOP."""
        from .directors import stop_intent
        from .port import DirectPort
        for request in Path(self.runs_dir).glob('ci-*.json'):
            saved = read_result(request) or {}
            spec = (saved.get('context') or {}).get('spec')
            if spec and saved.get('execution') != 'prepared':
                status = runner.read_status(self.runs_dir, spec['run_id']) or {}
                if status.get('nonce') == spec['nonce'] and status.get('state') in ('exited', 'killed', 'timeout'):
                    continue
                try:
                    slots.lease_pin(conn, saved.get('ticket'))
                except slots.SlotExpired:
                    saved['held_reason'] = 'termination-uncertain: capacity reservation missing; reconcile original execution'
                else:
                    saved['held_reason'] = 'termination-uncertain: stop requested'
                runner.write_json_atomic(request, saved)
                stop_intent(DirectPort(conn), spec, epoch,
                            lambda value: runner.request_stop(self.runs_dir, value['run_id'], nonce=value['nonce']))

    def resume(self, prog, checkout, *, conn=None, terminal_outcomes=False):
        """Finish any saved full CI before its checkout is rebuilt for a changed merge queue.

        The completed result remains available to the next run call for that exact
        request, so the unchanged group does not execute its script twice.
        """
        for request in Path(self.runs_dir).glob('ci-*.json'):
            saved = read_result(request)
            if saved and saved.get('checkout') == str(checkout):
                key = request.stem.removeprefix('ci-')
                result = self.run(prog, checkout, saved['commit'], kind=saved['kind'],
                                  platforms=saved['platforms'], base=saved['base'], conn=conn,
                                  priority=saved.get('priority', 'P1'), force_full=saved.get('force_full', False),
                                  module=saved.get('module'), tier=saved.get('tier'))
                self._resumed[str(checkout)] = (key, result)
                if result.get('outcome') == 'infrastructure-failed' and not terminal_outcomes:
                    raise CIInfrastructure(result)

    def run(self, prog, checkout, commit, *, kind='full', platforms=None, base=None, conn=None, priority='P1', control_epoch=None, force_full=False, module=None, tier=None):
        """Run placements WSL first in the shared checkout, or adopt a saved runner after a hub restart.

        Queued tickets can expire; a possibly-issued execution pins its acquired
        ticket until terminal reconciliation, even across STOP and long downtime.
        CIInterrupted retains the request and ticket; callers must keep the checkout
        until resume confirms that the saved execution has finished.
        """
        prog = repos.load(prog, commit, checkout)
        platforms = platforms or repos.gate_platforms(prog)
        key = hashlib.sha256(json.dumps([str(checkout), commit, kind, platforms, base]).encode()).hexdigest()[:24]
        if module:
            key += '-' + hashlib.sha256(json.dumps([module, tier]).encode()).hexdigest()[:12]
        if force_full:
            key += '-full'
        if priority != 'P1':
            key += '-' + priority
        request = Path(self.runs_dir) / f'ci-{key}.json'
        cached = self._resumed.pop(str(checkout), None)
        if cached and cached[0] == key:
            return cached[1]
        saved = read_result(request) or {'index': 0, 'results': [], 'checkout': str(checkout),
                                        'commit': commit, 'kind': kind, 'platforms': platforms, 'base': base, 'priority': priority,
                                        'order': repos.gate_order(platforms), 'force_full': force_full, 'module': module, 'tier': tier}
        from .launch import AdmissionHeld
        from .port import DirectPort
        from . import disk
        port = DirectPort(conn) if conn is not None else None
        if port is not None:
            saved.setdefault('control_epoch', port.admission()['epoch'] if control_epoch is None else control_epoch)
        def guard():
            if port is not None:
                port.require_admission(saved['control_epoch'])
        deferred = []
        def defer(error):
            error.detail.update(sha=commit, kind=kind, platform=spec['placement'], run_id=spec['run_id'], nonce=spec['nonce'])
            deferred.append(error)
            saved.pop('ticket', None)
            runner.write_json_atomic(request, saved)
            raise error
        # Legacy requests keep their saved order so an issued runner is never relabelled.
        order = saved.get('order', platforms)
        for i in range(saved['index'], len(order)):
            context = saved.get('context')
            # Old saved requests had no phase. Treat those as possibly issued.
            following = context is not None and saved.get('execution') != 'prepared'
            if context is None:
                guard()
                context = self.prepare(prog, checkout, commit, 'ci-' + uuid.uuid4().hex, kind=kind, platform=order[i], base=base,
                                       priority=priority, **({'force_full': True} if force_full else {}),
                                       **({'module': module, 'tier': tier} if module else {}))
                saved['context'] = context
                saved['execution'] = 'prepared'
                runner.write_json_atomic(request, saved)
            spec = dict(context['spec'])
            ticket = saved.get('ticket')
            def status_now():
                value = runner.read_status(self.runs_dir, spec['run_id'])
                return value if value and value.get('nonce') == spec['nonce'] else None
            status = status_now()
            following = following or status is not None
            terminal = status and status.get('state') in ('exited', 'killed', 'timeout')
            interrupted = False
            try:
                if following and conn is not None and not terminal:
                    # Preserve the original slot before generic acquisition may
                    # expire it. An already-lost legacy reservation needs explicit
                    # reconciliation, never a duplicate launch under a new ticket.
                    try:
                        slots.lease_pin(conn, ticket)
                    except slots.SlotExpired as error:
                        raise CIInterrupted('termination-uncertain: capacity reservation missing; reconcile original execution') from error
                if following and not status:
                    if port is not None:
                        control = port.admission(epoch=saved['control_epoch'])
                        if not control['allowed']:
                            self.stop_active(conn, control['epoch'])
                    raise CIInterrupted('termination-uncertain: missing nonce-matched runner status')
                if not following and disk.admission(disk.read())['verdict'] == 'hold':
                    defer(CIDeferred('CI deferred until a healthy disk reading'))
                if conn is not None and not terminal:
                    limits = slots.configured_limits(lambda k, default: ops.get_setting(conn, k, default))
                    while True:
                        if not following:
                            guard()
                        if ticket is None:
                            ticket = slots.lease_request(conn, key, priority, holder=spec['run_id'], platform=spec['placement'])
                            saved['ticket'] = ticket
                            runner.write_json_atomic(request, saved)
                        try:
                            if slots.lease_acquire(conn, ticket, *limits, ttl=600,
                                    windows_slots=slots.configured_windows_limit(lambda k, d: ops.get_setting(conn, k, d))) is not None:
                                break
                        except slots.SlotExpired:
                            if following:
                                raise CIInterrupted('termination-uncertain: capacity reservation missing')
                            ticket = None
                            continue
                        time.sleep(self.poll_s)
                if following:
                    status = status_now()
                    if not status:
                        raise CIInterrupted('termination-uncertain: missing nonce-matched runner status')
                if not following and disk.admission(disk.read())['verdict'] == 'hold':
                    defer(CIDeferred('CI deferred until a healthy disk reading'))
                if not following and kind == 'full':
                    from . import resources
                    resource = (self.resource_check or resources.check)(new_workers=1)
                    verdicts = [resource, *([resource.get('wsl', {})] if spec['placement'] == 'wsl' else []),
                                resource.get('ram_sides', {}).get(spec['placement'], {})]
                    blocked = [v for v in verdicts if v.get('verdict', 'go') != 'go']
                    if blocked:
                        reason = '; '.join(r for v in blocked for r in v.get('reasons', [])) or blocked[0]['verdict']
                        defer(CIDeferred('CI queued for resources: ' + reason))
                if following:
                    handle = self.launcher.adopt(spec)
                else:
                    guard()
                    spec['_admission_check'] = guard
                    if conn is not None:
                        slots.lease_pin(conn, ticket)
                    saved['execution'] = 'issued'
                    runner.write_json_atomic(request, saved)
                    following = True  # includes a lost launch response
                    try:
                        handle = self.launcher.launch(spec)
                    except AdmissionHeld:
                        # Only this new attempt is provably unissued. Refusing
                        # adoption of an older effect cannot clear its reservation.
                        following = False
                        raise
                execution = self._supervise(handle, spec, conn, port, ticket, saved, bool(status), commit)
                done = self._classify(spec, execution, saved, request, commit, i)
                if done is not None:
                    return done
            except BaseException as error:
                if deferred:
                    raise
                status = runner.read_status(self.runs_dir, spec['run_id']) or {}
                interrupted = following and (isinstance(error, CIInterrupted) or not (status.get('nonce') == spec['nonce']
                    and status.get('state') in ('exited', 'killed', 'timeout')))
                if interrupted:
                    # The runner owns its timeout. Keep its ticket and request so resume
                    # follows this execution before landing can rebuild the checkout.
                    saved['held_reason'] = 'termination-uncertain: ' + str(error)
                    runner.write_json_atomic(request, saved)
                    raise CIInterrupted(f'CI supervision interrupted; resume saved runner: {error}',
                        dict(sha=commit, platform=spec['placement'], kind=kind, run_id=spec['run_id'], nonce=spec['nonce'])) from error
                request.unlink(missing_ok=True)
                raise
            finally:
                if not interrupted and conn is not None and ticket is not None:
                    slots.lease_release(conn, ticket)
        # The final result certifies every placement; keep each individual result.
        result = {**saved['results'][-1], 'platform': '+'.join(platforms), 'results': saved['results']}
        if validate_receipt(result, {'sha': commit, 'kind': kind, 'platforms': platforms}):
            raise CIError('saved platform receipts do not match the full CI plan')
        request.unlink(missing_ok=True)
        return result

    def _supervise(self, handle, spec, conn, port, ticket, saved, was_following, commit):
        next_scan, missed = time.monotonic() + (0 if was_following else 30), 0
        while True:
            execution = handle.poll()
            if execution is not None:
                return execution
            if conn is not None:
                slots.lease_heartbeat(conn, ticket)
                control = port.admission(epoch=saved['control_epoch'])
                if not control['allowed']:
                    self.stop_active(conn, control['epoch'])
                    raise CIInterrupted('termination-uncertain: ' + control['reason'])
            if time.monotonic() >= next_scan:
                next_scan = time.monotonic() + 30
                try:
                    found = self.launcher.runners(want_wsl=spec['placement'] == 'wsl')
                except runner.ScanError:
                    found = None  # unknown is not absent; the runner still owns its timeout
                current = runner.read_status(self.runs_dir, spec['run_id']) or {}
                if found is not None and (str(spec['run_id']) in found or current.get('state') in ('exited', 'killed', 'timeout')):
                    missed = 0
                elif found is not None:
                    missed += 1
                    next_scan = time.monotonic() + .25
                    if missed >= 2:
                        runner.request_stop(self.runs_dir, spec['run_id'], nonce=spec['nonce'])
                        if current.get('nonce') != spec['nonce'] or not self.launcher.recover_ci(spec, current):
                            raise CIInterrupted('CI runner lost; child termination is not confirmed')
                        runner.write_json_atomic(runner.run_dir(self.runs_dir, spec['run_id']) / runner.STATUS_FILE,
                                                 {**current, 'state': 'killed', 'exit_code': None,
                                                  'ended': now(), 'reason': 'runner disappeared'})
                        handle.cancel()
                        raise CIInfrastructure(dict(status='failed', passed=False,
                            sha=commit, platform=spec['placement'], run_id=spec['run_id'], kind=saved['kind'],
                            nonce=spec['nonce'], outcome='infrastructure-failed', failures=[], reason='runner disappeared', log_tail=['CI runner disappeared before writing its result']))
            time.sleep(self.poll_s)

    def _classify(self, spec, execution, saved, request, commit, i):
        evaluated = self.result(spec['run_id'], execution, spec)
        receipt = evaluated['receipt']
        if receipt is not None:
            receipt = {**receipt, 'outcome': evaluated['outcome'], 'reason': evaluated['reason'], 'run_id': spec['run_id']}
        if evaluated.get('nonce') != spec.get('nonce') or evaluated.get('run_id') != spec.get('run_id'):
            raise CIInterrupted('termination-uncertain: saved plan does not match runner association')
        if evaluated.get('outcome') == 'termination-unknown':
            raise CIInterrupted(evaluated['reason'], evaluated)
        if evaluated.get('outcome') == 'deferred':
            request.unlink(missing_ok=True)
            return {**evaluated, 'status': 'refused'}
        if receipt is None or evaluated['outcome'] == 'infrastructure-failed':
            request.unlink(missing_ok=True)
            return {'outcome': 'infrastructure-failed', 'status': 'failed', 'passed': False, 'sha': commit, 'kind': saved['kind'], 'nonce': spec['nonce'], 'failures': [],
                    'platform': spec['placement'], 'run_id': spec['run_id'],
                    'reason': 'timeout' if execution.get('status') == 'timeout' else evaluated['reason'],
                    'log_tail': evaluated['excerpt'].splitlines(), 'receipt': receipt}
        saved['results'].append(receipt)
        saved['index'] = i + 1
        for field in ('context', 'ticket', 'execution', 'held_reason'):
            saved.pop(field, None)
        runner.write_json_atomic(request, saved)
        if evaluated['passed']:
            return None
        request.unlink(missing_ok=True)
        return receipt


def _execute(plan):
    """The runner's child: all script argv is executed verbatim, stdout inherited."""
    checkout, commit, base = plan['repo'], plan['sha'], plan['base']
    placement.prepare_worktree(checkout)
    def refuse(reason):
        runner.write_json_atomic(Path(plan['out']), dict(version=VERSION, sha=commit, passed=False,
                                 status=contract.CI_REFUSED, outcome='deferred', reason=reason, failures=[], kind=plan['kind'], run_id=plan.get('run_id'), nonce=plan.get('nonce'),
                                 platform=plan['platform'], exit_code=None, log_tail=[reason]))
        raise CIError(reason)
    if head(checkout) != commit:
        refuse(f'HEAD is not the planned commit {commit[:12]}')
    if dirty(checkout):
        refuse('refused: working tree has uncommitted changes; commit before CI')
    base_sha, has_base = contains(checkout, base, commit)
    started, steps = now(), []
    for argv in plan['commands']:
        code = subprocess.run(placement.ci_command(argv, plan.get('priority', 'P2')),
                              cwd=checkout, env=git_environment()).returncode
        steps.append({'command': argv, 'exit_code': code,
                      'status': contract.CI_PASSED if code == 0 else contract.CI_FAILED})
        if code:
            break
    unchanged = head(checkout) == commit and not dirty(checkout, post_run=True)
    passed = bool(steps) and steps[-1]['exit_code'] == 0 and unchanged
    receipt = dict(version=VERSION, sha=commit, repo=checkout, base_ref=base, base_sha=base_sha,
                   contains_base=has_base, kind=plan['kind'], platform=plan['platform'],
                   started=started, finished=now(), steps=steps, command=plan['commands'][0],
                   exit_code=steps[-1]['exit_code'], tree_unchanged=unchanged, passed=passed,
                   status=contract.CI_PASSED if passed else contract.CI_FAILED,
                   outcome='passed' if passed else 'code-failed',
                   reason='' if passed else ('CI command failed' if unchanged else 'working tree changed during CI'), failures=[],
                   run_id=plan.get('run_id'), nonce=plan.get('nonce'), log=plan['log'], log_tail=_tail(plan['log']))
    if not passed:
        try:
            failures = set()
            with open(plan['log'], encoding='utf-8', errors='replace') as stream:
                for line in stream:
                    if m := re.fullmatch(r'DROVER_CI_FAILURE (default|integration) (tests\.test_[A-Za-z0-9_]+)\s*', line):
                        failures.add(m.groups())
            receipt['failures'] = [{'tier': tier, 'module': module} for tier, module in sorted(failures)]
            if len(failures) == 1:
                receipt['tier'], receipt['module'] = next(iter(failures))
        except OSError:
            pass
    runner.write_json_atomic(Path(plan['out']), receipt)
    return 0 if passed else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    args = parser.parse_args(argv)
    try:
        return _execute(json.loads(Path(args.plan).read_text(encoding='utf-8')))
    except (CIError, OSError, ValueError, KeyError) as error:
        print(f'CI: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
