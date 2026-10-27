"""Checks and full CI: repo scripts on exact commits, supervised by the runner.

CI.prepare produces a runner spec for asynchronous checks. CI.run starts or adopts
full CI and waits for its result. Planning, docs classification, output and verdicts
are shared by both. No command or output format is inferred from a repository.
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
else:
    from . import placement, repos, runner, contract
    from .ledger import ci as slots, ops

VERSION = 1
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


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


# ---- changed paths and documentation guards ---------------------------------------------------------------

def changed_paths(repo, base, tip='HEAD'):
    return [p for p in git(repo, '-c', 'core.quotepath=false', 'diff', '--name-only', '--no-renames', '-z', f'{base}...{tip}').stdout.split('\0') if p]


def _norm(path):
    return str(path).replace('\\', '/')


def _glob_re(pattern):
    out, i, p = '', 0, _norm(pattern)
    while i < len(p):
        if p.startswith('**/', i):
            out += '(?:.*/)?'
            i += 3
        elif p.startswith('**', i):
            out += '.*'
            i += 2
        elif p[i] == '*':
            out += '[^/]*'
            i += 1
        elif p[i] == '?':
            out += '[^/]'
            i += 1
        else:
            out += re.escape(p[i])
            i += 1
    return re.compile(out + r'\Z')


DOCS_EXTENSIONS = ('.md', '.rst', '.txt', '.adoc')
DOCS_EXACT = ('LICENSE', 'GLOSSARY.md')


def _glob_hit(path, pattern):
    rx = _glob_re(pattern)
    return bool(rx.match(path) or ('/' not in _norm(pattern) and rx.match(path.rsplit('/', 1)[-1])))


def docs_only(paths, globs):
    """True when there is at least one path and every path is docs: it matches a positive glob, no `!` glob, and
    has a docs extension (.md .rst .txt .adoc) or is exactly LICENSE or GLOSSARY.md. `**` crosses directories, `*`
    does not; a glob without a slash matches the file name at any depth (`*.md` covers docs/a/b.md)."""
    paths = [_norm(p) for p in paths]
    positive = [g for g in globs or () if not g.startswith('!')]
    negative = [g[1:] for g in globs or () if g.startswith('!')]
    if not paths or not positive:
        return False
    return all((p in DOCS_EXACT or p.lower().endswith(DOCS_EXTENSIONS))
               and any(_glob_hit(p, g) for g in positive) and not any(_glob_hit(p, g) for g in negative)
               for p in paths)


def changed_docs_only(repo, base, tip, globs):
    """docs_only for `base...tip`, and false when any changed path is or was a symlink (git mode 120000)."""
    paths = changed_paths(repo, base, tip)
    if not docs_only(paths, globs):
        return False
    raw = git(repo, 'diff', '--raw', '--no-renames', '-z', f'{base}...{tip}').stdout
    return not any(f.startswith(':') and '120000' in f.lstrip(':').split()[:2] for f in raw.split('\0'))


def status_problems(receipt):
    """Version and outcome problems that need no repository."""
    found = []
    if receipt.get('version') != VERSION:
        found.append(f'unknown receipt version {receipt.get("version")}')
    if not receipt.get('passed'):
        found.append(f'gate did not pass (exit {receipt.get("exit_code")}, tree unchanged {receipt.get("tree_unchanged")})')
    return found


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


def read_result(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _tail(path, lines=40):
    # Read backwards so large CI logs do not grow the hub's memory use.
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, 2)
            end, chunks, count = stream.tell(), [], 0
            while end and count <= lines:
                start = max(0, end - 8192)
                stream.seek(start)
                chunk = stream.read(end - start)
                chunks.append(chunk)
                count += chunk.count(b'\n')
                end = start
        return b''.join(reversed(chunks)).decode('utf-8', errors='replace').splitlines()[-lines:]
    except OSError:
        return []


class CI:
    def __init__(self, runs_dir, launcher=None, poll_s=0.25, *, source=None):
        from .launch import RunnerLauncher
        self.source = source
        self.runs_dir = str(runs_dir)
        self.launcher = launcher or RunnerLauncher(runs_dir=self.runs_dir)
        self.poll_s = poll_s       # how often run() polls a runner and a slot queue; tests inject a short one
        self._resumed = {}

    def prepare(self, prog, checkout, commit, run_id, *, kind='checks', platform=None, base=None, priority='P2', force_full=False):
        """Plan a repo script at commit and return its runner spec and CI context."""
        source = self.source
        (source.prepare_worktree if source else placement.prepare_worktree)(checkout)
        prog = (source.load if source else repos.load)(prog, commit, checkout)
        policy = repos.ci_policy(prog)
        platform = platform or repos.gate_platforms(prog)[0]
        base = base or policy['base']
        paths = (source.changed_paths if source else changed_paths)(checkout, base, commit)
        docs = not force_full and policy['docs_only'] and (source.changed_docs_only if source else changed_docs_only)(
            checkout, base, commit, policy['docs_only_globs'])
        effective = 'docs' if docs else kind
        commands = []
        if docs:
            commands.append(['git', '-c', 'core.whitespace=-blank-at-eol', 'diff', '--check', f'{base}...{commit}'])
        if not docs or repos.has_script(prog, 'docs'):
            commands.append(repos.script_argv(prog, 'docs' if docs else 'test', platform))
        cfg = repos.run_config(prog)
        cfg['env'] = {**cfg['env'], **GIT_PROMPT_ENV, 'DROVER_CI_KIND': effective,
                      'DROVER_CHANGED_FILES': json.dumps(paths)}
        d = runner.run_dir(self.runs_dir, run_id)
        d.mkdir(parents=True, exist_ok=True)
        result_path = d / 'receipt.json'
        plan = {'repo': placement.ci_path(platform, checkout), 'sha': commit, 'base': base,
                'kind': effective, 'platform': platform, 'commands': commands, 'priority': priority,
                'out': placement.ci_path(platform, result_path),
                'log': placement.ci_path(platform, d / runner.OUT_FILE)}
        runner.write_json_atomic(d / 'plan.json', plan)
        spec = {'run_id': run_id, 'role': 'gate', 'backend': 'local', 'model': 'gate', 'effort': None,
                'argv': placement.ci_invocation(platform, Path(__file__), d / 'plan.json'),
                'cwd': str(checkout), 'placement': platform, 'nonce': uuid.uuid4().hex,
                'wall_s': policy['timeout_s'], 'idle_s': policy['idle_s'], 'user_prompt': '', 'run_config': cfg}
        return {'spec': spec, 'receipt': str(result_path), 'kind': effective, 'sha': commit,
                'files': paths, 'scope': None, 'diff_lines': (source.diff_lines if source else self._diff_lines)(checkout, base, commit)}

    @staticmethod
    def _diff_lines(checkout, base, commit):
        rows = git(checkout, 'diff', '--numstat', '--no-renames', f'{base}...{commit}').stdout.splitlines()
        return sum(int(n) for row in rows for n in row.split('\t')[:2] if n.isdigit())

    def result(self, run_id, execution=None):
        """Return the CI result and failure classification after a runner has finished."""
        d = runner.run_dir(self.runs_dir, run_id)
        receipt = read_result(d / 'receipt.json')
        st = runner.read_status(self.runs_dir, run_id) or {}
        execution = execution or {'status': st.get('state'), 'returncode': st.get('exit_code'),
                                  'timeout': st.get('reason')}
        timed_out = execution.get('status') == 'timeout'
        abnormal = execution.get('status') in ('error', 'timeout') or st.get('state') == 'killed'
        passed = bool(receipt and receipt.get('passed') and not abnormal
                      and execution.get('returncode') == 0)
        code = (receipt or {}).get('exit_code')
        infra = timed_out or not receipt or receipt.get('status') == contract.CI_REFUSED \
            or not (isinstance(code, int) and code >= 0) \
            or bool(receipt and receipt.get('passed') and (abnormal or execution.get('returncode') != 0))
        lines = []
        if timed_out:
            lines.append(f"CI timed out ({execution.get('timeout')})")
        elif not receipt:
            lines.append(f"CI refused or crashed (exit {execution.get('returncode')}):")
        else:
            lines.append(f"CI command exited {code}")
        lines += _tail(d / runner.OUT_FILE, contract.EXCERPT_LINES - len(lines))
        return {'receipt': receipt, 'passed': passed, 'infra': infra, 'excerpt': '\n'.join(lines)}

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
                                  priority=saved.get('priority', 'P1'), force_full=saved.get('force_full', False))
                self._resumed[str(checkout)] = (key, result)
                if result.get('infra') and not terminal_outcomes:
                    raise CIInfrastructure(result)

    def run(self, prog, checkout, commit, *, kind='full', platforms=None, base=None, conn=None, priority='P1', control_epoch=None, force_full=False):
        """Run placements WSL first in the shared checkout, or adopt a saved runner after a hub restart.

        Queued tickets can expire; a possibly-issued execution pins its acquired
        ticket until terminal reconciliation, even across STOP and long downtime.
        CIInterrupted retains the request and ticket; callers must keep the checkout
        until resume confirms that the saved execution has finished.
        """
        prog = repos.load(prog, commit, checkout)
        platforms = platforms or repos.gate_platforms(prog)
        key = hashlib.sha256(json.dumps([str(checkout), commit, kind, platforms, base]).encode()).hexdigest()[:24]
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
                                        'order': repos.gate_order(platforms), 'force_full': force_full}
        from .launch import AdmissionHeld
        from .port import DirectPort
        port = DirectPort(conn) if conn is not None else None
        if port is not None:
            saved.setdefault('control_epoch', port.admission()['epoch'] if control_epoch is None else control_epoch)
        def guard():
            if port is not None:
                port.require_admission(saved['control_epoch'])
        # Legacy requests keep their saved order so an issued runner is never relabelled.
        order = saved.get('order', platforms)
        for i in range(saved['index'], len(order)):
            context = saved.get('context')
            # Old saved requests had no phase. Treat those as possibly issued.
            following = context is not None and saved.get('execution') != 'prepared'
            if context is None:
                guard()
                rid = 'ci-' + uuid.uuid4().hex
                context = self.prepare(prog, checkout, commit, rid, kind=kind, platform=order[i], base=base,
                                       priority=priority, **({'force_full': True} if force_full else {}))
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
            from . import disk
            interrupted, capacity_deferred = False, False
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
                    capacity_deferred = True
                    saved.pop('ticket', None)
                    runner.write_json_atomic(request, saved)
                    raise CIInterrupted('CI deferred until a healthy disk reading')
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
                    capacity_deferred = True
                    saved.pop('ticket', None)
                    runner.write_json_atomic(request, saved)
                    raise CIInterrupted('CI deferred until a healthy disk reading')
                if not following and kind == 'full':
                    from . import resources
                    resource = resources.check(new_workers=1)
                    verdicts = [resource]
                    if spec['placement'] == 'wsl':
                        verdicts.append(resource.get('wsl', {}))
                    verdicts.append(resource.get('ram_sides', {}).get(spec['placement'], {}))
                    blocked = [v for v in verdicts if v.get('verdict', 'go') != 'go']
                    if blocked:
                        capacity_deferred = True
                        saved.pop('ticket', None)
                        runner.write_json_atomic(request, saved)
                        reason = '; '.join(r for v in blocked for r in v.get('reasons', []))
                        raise CIDeferred('CI queued for resources: ' + (reason or blocked[0]['verdict']))
                if not following:
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
                else:
                    handle = self.launcher.adopt(spec)
                next_scan, missed = time.monotonic() + (0 if status else 30), 0
                while True:
                    execution = handle.poll()
                    if execution is not None:
                        break
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
                            pass  # unknown is not absent; the runner still owns its timeout
                        else:
                            current = runner.read_status(self.runs_dir, spec['run_id']) or {}
                            if str(spec['run_id']) in found or current.get('state') in ('exited', 'killed', 'timeout'):
                                missed = 0
                            else:
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
                                    raise CIInfrastructure(dict(status='infra', infra=True, passed=False,
                                        sha=commit, platform=spec['placement'], run_id=spec['run_id'],
                                        reason='runner disappeared', log_tail=['CI runner disappeared before writing its result']))
                    time.sleep(self.poll_s)
                evaluated = self.result(spec['run_id'], execution)
                receipt = evaluated['receipt']
                if receipt is not None:
                    receipt = {**receipt, 'run_id': spec['run_id']}
                if receipt and receipt.get('status') == contract.CI_REFUSED:
                    raise CIError(receipt.get('reason') or evaluated['excerpt'])
                if receipt is None or evaluated['infra']:
                    request.unlink(missing_ok=True)
                    return {'status': 'infra', 'passed': False, 'infra': True, 'sha': commit,
                            'platform': spec['placement'], 'run_id': spec['run_id'],
                            'reason': 'timeout' if execution.get('status') == 'timeout' else 'infrastructure',
                            'log_tail': evaluated['excerpt'].splitlines(), 'receipt': receipt}
                saved['results'].append(receipt)
                saved['index'] = i + 1
                saved.pop('context', None)
                saved.pop('ticket', None)
                saved.pop('execution', None)
                saved.pop('held_reason', None)
                runner.write_json_atomic(request, saved)
                if not evaluated['passed']:
                    request.unlink(missing_ok=True)
                    return receipt
            except BaseException as error:
                if capacity_deferred:
                    raise
                status = runner.read_status(self.runs_dir, spec['run_id']) or {}
                interrupted = following and not (status.get('nonce') == spec['nonce']
                    and status.get('state') in ('exited', 'killed', 'timeout'))
                if interrupted:
                    # The runner owns its timeout. Keep its ticket and request so resume
                    # follows this execution before landing can rebuild the checkout.
                    saved['held_reason'] = 'termination-uncertain: ' + str(error)
                    runner.write_json_atomic(request, saved)
                    raise CIInterrupted(f'CI supervision interrupted; resume saved runner: {error}') from error
                request.unlink(missing_ok=True)
                raise
            finally:
                if not interrupted and conn is not None and ticket is not None:
                    slots.lease_release(conn, ticket)
        request.unlink(missing_ok=True)
        # The final result certifies every placement; keep each individual result.
        return {**saved['results'][-1], 'platform': '+'.join(platforms), 'results': saved['results']}


def _execute(plan):
    """The runner's child: all script argv is executed verbatim, stdout inherited."""
    checkout, commit, base = plan['repo'], plan['sha'], plan['base']
    placement.prepare_worktree(checkout)
    def refuse(reason):
        runner.write_json_atomic(Path(plan['out']), dict(version=VERSION, sha=commit, passed=False,
                                 status=contract.CI_REFUSED, reason=reason, kind=plan['kind'],
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
                   log=plan['log'], log_tail=_tail(plan['log']))
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
