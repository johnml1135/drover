"""Gates: receipts per exact SHA, scope selection, a ranked lease in the ledger, mechanical failure parsing.

python -m drover.gate run --out gate-<sha>.json [--repo DIR] [--base origin/main] [--auto-scope MAP] -- <command>
python -m drover.gate verify --receipt gate-<sha>.json [--repo DIR] [--base origin/main]
python -m drover.gate scope --map MAP.json [--repo DIR] [--base origin/main]
python -m drover.gate parse LOG [--diff FILE ...]

A receipt passes only when the command exited 0 and the tree was clean and on the same HEAD before and after.
With retry_failed, each failing test class is rerun alone; if every one passes the status is `passed_with_flakes`.
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from .winproc import NO_WINDOW

VERSION = 1
PRIORITIES = ('P0', 'P1', 'P2', 'P3', 'P4')
DEFAULT_FAILURE_PATTERN = r'^\s+Failed (\S+) \['
MAX_RETRY_CLASSES = 12
SUMMARY = re.compile(r'(?:Passed|Failed)!\s+-\s+Failed:\s*(\d+),\s*Passed:\s*(\d+)')
TROUBLE_TEXT = (r"the active test run was aborted|test host process crashed|test run aborted"
                r"|the test host.*(?:crashed|exited)|testhost.*(?:crashed|exited unexpectedly)"
                r"|(?:blame|data collector 'blame' message)\W.*(?:hang|crash|abort)")
# dotnet's own wording only: a failing test's message may say "timed out" or "abort" and still be a plain flake.
TROUBLE = re.compile(TROUBLE_TEXT, re.I)
STDERR_TROUBLE = re.compile(TROUBLE_TEXT + r"|fatal error|stack overflow|unhandled exception|process terminated", re.I)
TEST_LINE = re.compile(r'^\s*(?:Failed|Passed|Skipped)\s+\S+\s+\[|^\s*\[xUnit\.net[^\]]*\]\s+\S+\s+\[(?:FAIL|SKIP|PASS)\]')
LOG_LINE = re.compile(r'^\s+log:\s+(\S.*?)\s*$', re.MULTILINE)
TOTALS = re.compile(r'^\s+all projects\s+[\d,.]+ s\s+passed\s+(\d+)\s+failed\s+(\d+)', re.MULTILINE)
GATE_TROUBLE = re.compile(r'^\s+(?:stopped:|reported zero test results)', re.MULTILINE)
KILL_CODES = (124, 137, 143)
GIT_PROMPT_ENV = {'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never', 'GIT_ASKPASS': ''}
UNITTEST_TRACEBACK_HOOK = ("import faulthandler,runpy,sys; "
                           "faulthandler.dump_traceback_later(500, exit=False); "
                           "sys.argv=['unittest', *sys.argv[1:]]; "
                           "runpy.run_module('unittest', run_name='__main__')")
CLASS_NAME = re.compile(r'^[A-Za-z_][\w.+]*$')
PROJECT_NAME = re.compile(r'^[A-Za-z_][\w.]*$')
# -f and -fi stay out: pwsh reads them as -File, and a full gate is launched with it.
NARROWING = re.compile(r'(?i)^--?(?:p(?:r(?:o(?:j(?:e(?:c(?:t)?)?)?)?)?)?|fil(?:t(?:e(?:r)?)?)?)(?:[:=].*)?$')
WEIGHTS = {'P0': 1000, 'P1': 1000, 'P2': 100, 'P3': 30, 'P4': 10}


class GateError(RuntimeError):
    pass


# ---- running a command ----------------------------------------------------------------------------------

def _default_interrupt():
    """A gate launched with `cmd &` inherits SIGINT ignored, and its tests of Ctrl+C then hang; give the gate the default."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGQUIT, signal.SIG_DFL)


def systemd_run_works():
    """True on Linux when `systemd-run --user --scope` can start a process; anything else runs unwrapped."""
    if not sys.platform.startswith('linux') or not shutil.which('systemd-run'):
        return False
    try:
        probe = subprocess.run(['systemd-run', '--user', '--scope', '--quiet', 'true'], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def wrap_command(command, priority, memory_max='8G', max_seconds=1800, available=None):
    """(argv to run, wrapper argv or None). CPU and IO weights by class; P4 also gets a memory cap and timeout."""
    if available is None:
        available = systemd_run_works()
    if not available:
        return list(command), None
    weight = WEIGHTS[priority]
    wrapper = ['systemd-run', '--user', '--scope', '--quiet', '-p', f'CPUWeight={weight}', '-p', f'IOWeight={weight}']
    inner = list(command)
    if priority == 'P4':
        wrapper += ['-p', f'MemoryMax={memory_max}']
        inner = ['timeout', str(max_seconds)] + inner
    return wrapper + ['--'] + inner, wrapper


def _spawn(argv, repo, stream, env=None):
    kw = {'preexec_fn': _default_interrupt} if os.name == 'posix' else {}
    command = _with_unittest_traceback(argv)
    return subprocess.run(command, cwd=repo, stdout=stream, stderr=subprocess.STDOUT,
                          env=git_environment(env), **kw).returncode


def _with_unittest_traceback(argv):
    """Schedule a diagnostic traceback for direct `python -m unittest` gate commands."""
    command = list(argv)
    start = 0
    if command[:1] == ['systemd-run'] and '--' in command:
        start = command.index('--') + 1
    if command[start:start + 1] == ['timeout'] and len(command) > start + 2:
        start += 2
    inner = command[start:]
    if len(inner) < 3 or not re.fullmatch(r'(?i)(?:python(?:w)?(?:\d+(?:\.\d+)?)?(?:\.exe)?|py(?:\.exe)?)',
                                          Path(inner[0]).name):
        return command
    module = next((i for i in range(1, len(inner) - 1) if inner[i:i + 2] == ['-m', 'unittest']), None)
    if module is None:
        return command
    instrumented = [inner[0], *inner[1:module], '-c', UNITTEST_TRACEBACK_HOOK, 'unittest', *inner[module + 2:]]
    return command[:start] + instrumented


def git_environment(base=None):
    """Environment that keeps Git noninteractive even when a machine has an interactive credential helper."""
    env = os.environ.copy() if base is None else dict(base)
    env.update(GIT_PROMPT_ENV)
    return env


def _remove_stale_gitfix_temp():
    """Remove only Drover's own abandoned git fixture directories, and only after a full day."""
    cutoff = time.time() - 24 * 60 * 60
    for path in Path(tempfile.gettempdir()).glob('drover-fix-*'):
        try:
            if path.lstat().st_mtime >= cutoff:
                continue
            if path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                shutil.rmtree(path)
        except OSError:
            continue


@contextmanager
def _gate_test_environment():
    """Give tests a parent for temporary fixtures; run_gate owns and removes it unless already provided."""
    env = git_environment()
    parent = env.get('DROVER_TEST_TMPDIR')
    owned = not parent
    if owned:
        _remove_stale_gitfix_temp()
        parent = tempfile.mkdtemp(prefix='drover-fix-gate-')
    else:
        Path(parent).mkdir(parents=True, exist_ok=True)
    env['DROVER_TEST_TMPDIR'] = str(parent)
    try:
        yield env
    finally:
        if owned:
            shutil.rmtree(parent, ignore_errors=True)


# ---- git ------------------------------------------------------------------------------------------------

def git(repo, *args, check=True):
    try:
        result = subprocess.run(['git', '-C', str(repo), *args], text=True, capture_output=True,
                                errors='replace', env=git_environment(), timeout=60, **NO_WINDOW)
    except subprocess.TimeoutExpired as error:
        raise GateError(f'git {" ".join(args)} timed out after 60 seconds') from error
    if check and result.returncode:
        raise GateError(f'git {" ".join(args)}: {result.stderr.strip()}')
    return result


def head(repo):
    return git(repo, 'rev-parse', 'HEAD').stdout.strip()


def dirty(repo):
    # Ignored build output is fine; tracked edits and new unignored files are not part of the tested commit.
    return git(repo, 'status', '--porcelain', '--untracked-files=normal').stdout.strip()


def contains(repo, base_ref, sha):
    base = git(repo, 'rev-parse', '--verify', '--quiet', base_ref + '^{commit}', check=False).stdout.strip()
    if not base:
        return None, False
    return base, git(repo, 'merge-base', '--is-ancestor', base, sha, check=False).returncode == 0


def pin(name):
    value = os.environ.get(name)
    entry = {'name': name, 'value': value}
    if value and Path(value).is_file():
        entry['sha256'] = hashlib.sha256(Path(value).read_bytes()).hexdigest()
    return entry


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


# ---- scope: which test projects a diff needs ---------------------------------------------------------------

def changed_paths(repo, base, tip='HEAD'):
    return [p for p in git(repo, 'diff', '--name-only', '--no-renames', f'{base}...{tip}').stdout.splitlines() if p]


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
    return not any(f.startswith(':') and '120000' in f.split()[:2] for f in raw.split('\0'))


def docs_commands(base, sha, extra=None):
    """The docs gate: `git diff --check base...sha`, then the repo's optional gate.commands.docs argv."""
    return [['git', '-c', 'core.whitespace=-blank-at-eol', 'diff', '--check', f'{base}...{sha}']] + ([list(extra)] if extra else [])


def load_map(path=None):
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise GateError(f'cannot read scope map {path}: {exc}')
    for key in ('always', 'fallback'):
        if not isinstance(data.get(key, []), list):
            raise GateError(f'scope map "{key}" must be a list')
    if not isinstance(data.get('patterns', {}), dict):
        raise GateError('scope map "patterns" must be an object')
    return data


def compute_scope(paths, scope_map):
    """{'projects': [...], 'unmatched': [paths], 'fallback_used': bool, 'paths': n}.

    A path that matches no pattern selects the fallback list, so an unmapped change widens the gate rather than
    skipping it. A pattern mapped to [] still matches: the path needs no test project.
    """
    projects = list(scope_map.get('always', []))
    unmatched = []
    for path in paths:
        matched = [plist for glob, plist in scope_map.get('patterns', {}).items() if fnmatch.fnmatch(path, glob)]
        if not matched:
            unmatched.append(path)
        projects += [p for plist in matched for p in plist]
    if unmatched:
        projects += scope_map.get('fallback', [])
    return {'projects': list(dict.fromkeys(projects)), 'unmatched': unmatched, 'fallback_used': bool(unmatched),
            'paths': len(paths)}


def scope_for(repo, base, map_path=None):
    return compute_scope(changed_paths(repo, base), load_map(map_path))


# ---- failure parsing ---------------------------------------------------------------------------------------

def failing_methods(log_text, pattern=DEFAULT_FAILURE_PATTERN):
    """Failing test names in log order, parameters dropped, duplicates kept out."""
    names = (m.group(1).split('(')[0] for m in re.finditer(pattern, log_text, re.MULTILINE))
    return list(dict.fromkeys(names))


def failing_classes(log_text, pattern=DEFAULT_FAILURE_PATTERN):
    """Failing test classes in log order: the fully qualified name minus its method, parameters dropped."""
    found = []
    for m in re.finditer(pattern, log_text, re.MULTILINE):
        name = m.group(1).split('(')[0]
        if '.' in name:
            found.append(name.rsplit('.', 1)[0])
    return list(dict.fromkeys(found))


def project_of(class_name, projects, project_pattern=""):
    """The test project owning a class: the longest scoped project that prefixes it, else the pattern's match."""
    owners = [p for p in projects if class_name.startswith(p + '.')]
    if owners:
        return max(owners, key=len)
    match = re.match(project_pattern, class_name) if project_pattern else None
    return match.group(1) if match else ''


def failing_count(log_text, pattern):
    return len(re.findall(pattern, log_text, re.MULTILINE))


DOTNET_FAILED = re.compile(r'^\s+Failed\s+(.+?)\s+\[[^\]]*\]\s*$')
DOTNET_NEXT = re.compile(r'^\s+(?:Failed|Passed|Skipped)\s+.+?\s+\[[^\]]*\]\s*$|^(?:Failed|Passed)!\s')
DOTNET_FRAME = re.compile(r'\bin\s+(.+?):line\s+\d+')
BUILD_ERROR = re.compile(r'^(?:\s*\d+>)?\s*(.+?)\((\d+),(\d+)\):\s+error\s+(\w+):\s+(.*?)(?:\s+\[[^\]]*\])?\s*$')
PYTEST_SUMMARY = re.compile(r'^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s+(.*))?$')
PYTEST_SECTION = re.compile(r'^_{3,}\s+(.+?)\s+_{3,}$')
PYTEST_LOC = re.compile(r'^(\S+\.py):\d+:\s')
UNITTEST_HEAD = re.compile(r'^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)')
PY_FRAME = re.compile(r'^\s+File "(.+?)", line \d+')
RULE = re.compile(r'^(?:-{20,}|={20,})$')


def _norm(path):
    return str(path).replace('\\', '/')


def path_matches(file, diff_path):
    """True when `file` is `diff_path`. A relative file must equal it; an absolute one (a stack frame) may end with it
    on a path-component boundary, so `util.py` in the diff never matches `lib/util.py`."""
    f, d = _norm(file), _norm(diff_path)
    d = d[2:] if d.startswith('./') else d
    if f == d:
        return True
    absolute = f.startswith('/') or re.match(r'^[A-Za-z]:/', f) is not None
    return absolute and f.endswith('/' + d)


def _in_diff(file, diff):
    hits = [_norm(d) for d in diff if path_matches(file, d)]
    return max(hits, key=len) if hits else None


def _guess_file(test, diff):
    """Name a diff file after the test class (Ns.FooTests.Method -> FooTests.cs); only when exactly one file has that stem."""
    parts = re.split(r'[.:]+', test)
    for cand in reversed(parts[:-1]):
        hits = {_norm(d) for d in diff if Path(d).stem == cand}
        if len(hits) == 1:
            return hits.pop()
    return None


def _finish(raw, diff):
    out = []
    for item in raw:
        file = item.get('file')
        hit = _in_diff(file, diff) if file else None
        if hit:
            file = hit
        elif not file:
            file = _guess_file(item['test'], diff)
            hit = file
        out.append({'test': item['test'], 'error': (item.get('error') or '').strip()[:300], 'file': file,
                    'in_diff': bool(hit)})
    return out


def _dotnet(lines):
    found, i = [], 0
    while i < len(lines):
        m = DOTNET_FAILED.match(lines[i])
        if not m:
            i += 1
            continue
        rec = {'test': m.group(1).split('(')[0], 'error': None, 'file': None}
        i += 1
        mode = None
        while i < len(lines) and not DOTNET_NEXT.match(lines[i]):
            text = lines[i].strip()
            if text == 'Error Message:':
                mode = 'msg'
            elif text == 'Stack Trace:':
                mode = 'stack'
            elif mode == 'msg' and text and not rec['error']:
                rec['error'] = text
            elif mode == 'stack' and not rec['file']:
                f = DOTNET_FRAME.search(text)
                rec['file'] = f.group(1) if f else None
            i += 1
        found.append(rec)
    return found


def _xunit(lines, seen):
    out = []
    for line in lines:
        m = re.match(r'^\s*\[xUnit\.net[^\]]*\]\s+(\S+)\s+\[FAIL\]', line)
        if m and m.group(1).split('(')[0] not in seen:
            out.append({'test': m.group(1).split('(')[0], 'error': None, 'file': None})
    return out


def _build_errors(lines):
    out, seen = [], set()
    for line in lines:
        m = BUILD_ERROR.match(line)
        if m and (m.group(1), m.group(4), m.group(2)) not in seen:
            seen.add((m.group(1), m.group(4), m.group(2)))
            out.append({'test': 'build', 'error': f'{m.group(4)}: {m.group(5)}', 'file': m.group(1)})
    return out


def _pytest(lines):
    found = []
    for line in lines:
        m = PYTEST_SUMMARY.match(line.strip())
        if m and '::' in m.group(1):
            found.append({'test': m.group(1), 'error': m.group(2), 'file': m.group(1).split('::')[0]})
    if found:
        return found
    i = 0
    while i < len(lines):  # no short summary (-rN): read the failure sections
        m = PYTEST_SECTION.match(lines[i])
        i += 1
        if not m:
            continue
        rec = {'test': m.group(1), 'error': None, 'file': None}
        while i < len(lines) and not PYTEST_SECTION.match(lines[i]) and not lines[i].startswith('====='):
            e = re.match(r'^E\s+(.*)', lines[i])
            if e and not rec['error']:
                rec['error'] = e.group(1)
            loc = PYTEST_LOC.match(lines[i])
            if loc:
                rec['file'] = loc.group(1)
            i += 1
        found.append(rec)
    return found


def _unittest(lines):
    found, i = [], 0
    while i < len(lines):
        m = UNITTEST_HEAD.match(lines[i])
        i += 1
        if not m:
            continue
        rec = {'test': f'{m.group(2)}.{m.group(1)}'.replace(f'.{m.group(1)}.{m.group(1)}', f'.{m.group(1)}'),
               'error': None, 'file': None}
        block = []
        if i < len(lines) and RULE.match(lines[i]):
            i += 1
        while i < len(lines) and not UNITTEST_HEAD.match(lines[i]) and not RULE.match(lines[i]):
            block.append(lines[i])
            i += 1
        frames = [f.group(1) for f in map(PY_FRAME.match, block) if f]
        mine = [f for f in frames if 'lib/python' not in _norm(f) and 'site-packages' not in _norm(f)
                and not _norm(f).endswith('/unittest/case.py')]
        rec['file'] = (mine or frames or [None])[-1]
        text = [b for b in block if b.strip() and not b.startswith(' ') and not b.startswith('Traceback')]
        rec['error'] = text[-1] if text else None
        found.append(rec)
    return found


def parse_failures(log_text, diff_files=()):
    """Gate output -> [{test, error, file, in_diff}], mechanically (dotnet, MSBuild errors, pytest, unittest).

    An empty list from a failing gate means the parser did not recognise the output: see `unparsed`.
    """
    lines = log_text.splitlines()
    raw = _dotnet(lines)
    raw += _xunit(lines, {r['test'] for r in raw})
    raw += _build_errors(lines) + _pytest(lines) + _unittest(lines)
    seen, unique = set(), []
    for r in raw:
        if r['test'] != 'build' and r['test'] in seen:
            continue
        seen.add(r['test'])
        unique.append(r)
    return _finish(unique, list(diff_files))


def unparsed(failures, exit_code):
    """True when the gate failed but nothing was parsed: the only case that needs a triage agent."""
    return exit_code != 0 and not failures


# ---- retry logic (ported from gate-receipt.py) -----------------------------------------------------------

def summary_counts(log_text):
    """(failed, passed) summed over every dotnet `Failed:/Passed:` summary line, and how many lines there were."""
    found = SUMMARY.findall(log_text)
    return sum(int(f) for f, _ in found), sum(int(p) for _, p in found), len(found)


def trouble_in(text, pattern=TROUBLE):
    """The first dotnet crash, abort or hang marker on a line that is not a test-name line, else None."""
    for line in text.splitlines():
        if not TEST_LINE.match(line):
            found = pattern.search(line)
            if found:
                return found.group(0)
    return None


def process_refusal(text, pattern, name):
    """Why one dotnet process log does not account for its own failures, else None."""
    trouble = trouble_in(text)
    if trouble:
        return f'{name}: log shows a crash, abort, hang or timeout marker ({trouble!r})'
    failed, _, lines = summary_counts(text)
    if not lines:
        return f'{name}: no dotnet Failed/Passed summary line, so the process may not have finished'
    parsed = failing_count(text, pattern)
    if failed != parsed:
        return f'{name}: summary counts {failed} failed tests, the failure pattern parsed {parsed}'
    return None


def failure_evidence(repo, log_text, code, pattern, expected_runs=1):
    """(text to read failing classes from, refusal). Reads each per-process log a test.ps1 gate names with `log:`."""
    if code in KILL_CODES:
        return log_text, f'gate exit {code} looks like a timeout or kill'
    paths = LOG_LINE.findall(log_text)
    if not paths:
        return log_text, process_refusal(log_text, pattern, 'gate log')
    if GATE_TROUBLE.search(log_text):
        return log_text, 'test.ps1 reports a stopped process or one with zero test results'
    runs = len(TOTALS.findall(log_text))
    if runs < expected_runs:
        return log_text, (f'{runs} test.ps1 totals lines for {expected_runs} expected runs; a scoped gate that stops '
                          'at its first failure leaves later projects untested')
    if not runs:
        return log_text, 'test.ps1 printed no `all projects` totals line, so the run did not finish'
    texts, failing_processes = [], 0
    for path in paths:
        stem = Path(path)
        stem = stem if stem.is_absolute() else Path(repo) / stem
        try:
            text = stem.read_text(errors='replace')
        except OSError:
            return log_text, f'cannot read process log {path}'
        refusal = process_refusal(text, pattern, stem.name)
        stderr = stem.with_name(stem.name[:-4] + '.stderr.log') if stem.name.endswith('.log') else None
        if not refusal and stderr and stderr.exists() and trouble_in(stderr.read_text(errors='replace'), STDERR_TROUBLE):
            refusal = f'{stderr.name}: stderr shows a crash, abort, hang or timeout marker'
        if refusal:
            return log_text, refusal
        failing_processes += summary_counts(text)[0] > 0
        texts.append(text)
    nameless = [n for text in texts for n in failing_methods(text, pattern) if '.' not in n]
    if nameless:
        return log_text, f'failing test {nameless[0]} has no class part to rerun'
    if failing_processes != len(paths):
        return log_text, f'{len(paths)} processes did not pass but {failing_processes} logs report failed tests'
    return '\n'.join(texts), None


def retry_passed(log_text):
    """A rerun passed only on test.ps1's own totals (Passed >= 1, Failed 0) or dotnet summaries of the same shape."""
    if GATE_TROUBLE.search(log_text) or LOG_LINE.search(log_text):
        return False
    totals = TOTALS.search(log_text)
    if totals:
        return int(totals.group(1)) >= 1 and int(totals.group(2)) == 0
    failed, passed, lines = summary_counts(log_text)
    return lines > 0 and failed == 0 and passed >= 1


def trx_outcomes(repo, since):
    """{test name without parameters: set of outcomes} from every TRX under bin/*/test-results written since `since`."""
    import xml.etree.ElementTree as ET
    found = {}
    for trx in Path(repo).glob('bin/*/test-results/**/*.trx'):
        try:
            if trx.stat().st_mtime < since:
                continue
            for element in ET.parse(trx).iter():
                if element.tag.rsplit('}', 1)[-1] == 'UnitTestResult' and element.get('testName'):
                    found.setdefault(element.get('testName').split('(')[0], set()).add(element.get('outcome'))
        except (OSError, ET.ParseError):
            continue
    return found


def methods_passed(repo, methods, retry_text, since):
    """True when every originally failed method shows as passed in the rerun: a `Passed` line or a TRX outcome."""
    outcomes = trx_outcomes(repo, since)
    for method in methods:
        listed = re.search(r'^\s*Passed\s+' + re.escape(method) + r'\b', retry_text, re.MULTILINE)
        if not listed and outcomes.get(method) != {'Passed'}:
            return False
    return True


def _split(text):
    return shlex.split(text, posix=os.name != 'nt')   # posix mode would eat the backslashes of a Windows path


def expand(template, name, owner):
    """Template tokens with {class} and {project} replaced; other braces stay literal. None when a value is unsafe."""
    if not CLASS_NAME.match(name) or ('{project}' in template and not PROJECT_NAME.match(owner)):
        return None
    return [t.replace('{class}', name).replace('{project}', owner) for t in _split(template)]


def retry_failures(repo, log, log_text, code, projects, *, retry_failed, retry_command, failure_pattern, priority,
                   memory_max, max_seconds, expect_runs, available, project_pattern="", env=None):
    """Rerun each failing class alone. -> (retries {class: [attempt...]}, flakes, failed classes, refusal)."""
    expected = expect_runs or max(1, len(projects))
    evidence, refusal = failure_evidence(repo, log_text, code, failure_pattern, expected)
    methods = failing_methods(evidence, failure_pattern)
    classes = failing_classes(evidence, failure_pattern)
    retries, flakes = {}, []
    if not refusal and any('.' not in m for m in methods):
        refusal = 'a failing test name has no class part to rerun'
    if refusal:
        return retries, flakes, classes, refusal
    if not classes or len(classes) > MAX_RETRY_CLASSES:
        return retries, flakes, classes, None
    for number, name in enumerate(classes, 1):
        owner = project_of(name, projects, project_pattern)
        attempts = retries[name] = []
        for attempt in range(1, retry_failed + 1):
            command = expand(retry_command, name, owner)
            if command is None:
                attempts.append(dict(attempt=attempt, exit_code=None, error='unsafe class or project name'))
                break
            path = log.with_name(f'{log.stem}.retry{number}-{attempt}.log')
            argv, _ = wrap_command(command, priority, memory_max, max_seconds, available)
            since = time.time() - 2
            with open(path, 'w') as stream:
                rerun = _spawn(argv, repo, stream, env=env)
            retry_text = path.read_text(errors='replace')
            mine = [m for m in methods if m.rsplit('.', 1)[0] == name]
            confirmed = rerun == 0 and retry_passed(retry_text) and methods_passed(repo, mine, retry_text, since)
            attempts.append(dict(attempt=attempt, exit_code=rerun, confirmed=confirmed, command=command,
                                 project=owner, log=str(path)))
            if confirmed:
                flakes.append(name)
                break
    return retries, flakes, classes, None


# ---- receipts ----------------------------------------------------------------------------------------------

def run_gate(repo, out, command, *, base='origin/main', pins=(), priority='P2', memory_max='8G', max_seconds=1800,
             auto_scope=None, expect_runs=0, retry_failed=0, retry_command=None,
             failure_pattern=DEFAULT_FAILURE_PATTERN, project_pattern="", log=None, available=None):
    """Run `command` in `repo`, write the JSON receipt to `out` and return it. `available=False` skips systemd-run."""
    repo = Path(repo).resolve()
    command = [str(c) for c in command]
    if not command:
        raise GateError('missing gate command')
    if retry_failed:
        if not retry_command:
            raise GateError('retry_failed needs retry_command')
        try:
            tokens = _split(retry_command)
        except ValueError as error:
            raise GateError(f'retry command does not parse: {error}')
        if '{class}' not in retry_command or not tokens:
            raise GateError('retry command must contain {class}')
    if dirty(repo):
        raise GateError('refused: working tree has uncommitted changes; commit the task before gating it')
    sha = head(repo)
    base_sha, has_base = contains(repo, base, sha)
    scope = None
    if auto_scope:
        scope = scope_for(repo, base, auto_scope)
        command += scope['projects']
    out = Path(out).resolve()
    log = Path(log).resolve() if log else out.with_suffix('.log')
    out.parent.mkdir(parents=True, exist_ok=True)
    started = now()
    argv, wrapper = wrap_command(command, priority, memory_max, max_seconds, available)
    retries, flakes, failed_classes, refusal = {}, [], [], None
    with _gate_test_environment() as child_env:
        with open(log, 'w') as stream:
            code = _spawn(argv, repo, stream, env=child_env)
        if code != 0 and retry_failed:
            retries, flakes, failed_classes, refusal = retry_failures(
                repo, log, log.read_text(errors='replace'), code, list(scope['projects']) if scope else [],
                retry_failed=retry_failed, retry_command=retry_command, failure_pattern=failure_pattern,
                priority=priority, memory_max=memory_max, max_seconds=max_seconds, expect_runs=expect_runs,
                available=available, project_pattern=project_pattern, env=child_env)
    unchanged = head(repo) == sha and not dirty(repo)
    tail = log.read_text(errors='replace').splitlines()[-40:]
    if code == 0:
        status = 'passed' if unchanged else 'failed'
    else:
        status = 'passed_with_flakes' if unchanged and failed_classes and set(flakes) == set(failed_classes) else 'failed'
    receipt = dict(version=VERSION, sha=sha, repo=str(repo), base_ref=base, base_sha=base_sha, contains_base=has_base,
                   command=command, pins=[pin(n) for n in pins], started=started, finished=now(),
                   exit_code=code, priority=priority, priority_enforced=wrapper is not None,
                   wrapper=wrapper, scope=scope, tree_unchanged=unchanged,
                   passed=code == 0 and unchanged, status=status, log=str(log), log_tail=tail)
    if retry_failed:
        receipt.update(original_exit_code=code, retry_failed=retry_failed, failed_classes=failed_classes,
                       retries=retries, flakes=flakes, retry_refused=refusal)
    out.write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def status_problems(receipt):
    """Version and outcome problems that need no repository; `passed_with_flakes` counts as a pass here."""
    found = []
    if receipt.get('version') != VERSION:
        found.append(f'unknown receipt version {receipt.get("version")}')
    flaky = receipt.get('status') == 'passed_with_flakes'
    if flaky and (not receipt.get('tree_unchanged') or not receipt.get('flakes')):
        found.append('passed_with_flakes receipt lacks flakes or an unchanged tree')
    elif not receipt.get('passed') and not flaky:
        found.append(f'gate did not pass (exit {receipt.get("exit_code")}, tree unchanged {receipt.get("tree_unchanged")})')
    return found


def declaring_file(repo, base, sha, short_name):
    """The first file changed in base...sha that declares `class <short_name>`, else None."""
    names = git(repo, 'diff', '--name-only', '--no-renames', f'{base}...{sha}', check=False).stdout.splitlines()
    pattern = re.compile(r'\bclass\s+' + re.escape(short_name) + r'\b')
    for name in names:
        shown = git(repo, 'show', f'{sha}:{name}', check=False)
        if shown.returncode == 0 and pattern.search(shown.stdout):
            return name
    return None


def flake_problems(receipt, repo, base, sha):
    """A flaky class declared in a file the landing diff changes is not an unrelated flake: refuse it."""
    if receipt.get('status') != 'passed_with_flakes':
        return []
    found = []
    for name in receipt.get('flakes') or []:
        where = declaring_file(repo, base, sha, name.rsplit('.', 1)[-1])
        if where:
            found.append(f'flaky test class {name} is declared in {where}, which {base[:12]}...{sha[:12]} changes')
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
    else:
        found += flake_problems(receipt, repo, base, current)
    return found


def command_names(command, project):
    """True when an argv token, or one item of a comma-separated token, is exactly the project name."""
    return any(part == project for token in command for part in str(token).split(','))


def full_gate_problems(parent, marker):
    """Why `parent` is not a full gate: it was scoped, narrowed to a project or filter, or lacks the full-suite flag."""
    found = []
    if parent.get('scope') is not None:
        found.append('parent receipt is auto-scoped; the parent must be a full gate')
    command = [str(t) for t in parent.get('command') or []]
    if any(NARROWING.match(t) for t in command):
        found.append('parent receipt command names -Project or -Filter (or an abbreviation); the parent must be a full gate')
    if marker and marker not in command:
        found.append(f'parent receipt command lacks {marker}; the parent must be a full gate')
    return found


def chain_problems(scoped, parent, repo, base_ref, map_path=None, full_marker='-All'):
    """Why `parent` (a full receipt for A) does not cover `scoped` (a receipt for B = A + one commit). [] = it does."""
    found = [f'parent receipt: {t}' for t in status_problems(parent)]
    found += full_gate_problems(parent, full_marker)
    parent_sha, sha = parent.get('sha'), scoped.get('sha')
    if scoped.get('status') == 'passed_with_flakes':
        found.append('scoped receipt must pass outright; only the parent may carry flakes')
    parents = git(repo, 'rev-list', '--parents', '-n', '1', str(sha), check=False).stdout.split()[1:]
    if parents != [parent_sha]:
        found.append(f'{str(sha)[:12]} is not exactly one commit on top of parent receipt commit {str(parent_sha)[:12]}')
        return found
    base, has_base = contains(repo, base_ref, sha)
    if base is not None and has_base:
        found += [f'parent receipt: {t}' for t in flake_problems(parent, repo, base, sha)]
    changed = git(repo, 'diff', '--name-only', '--no-renames', parent_sha, sha, check=False).stdout.splitlines()
    needed = compute_scope([n for n in changed if n], load_map(map_path))['projects']
    command = scoped.get('command') or []
    missing = [p for p in needed if not command_names(command, p)]
    if missing:
        found.append(f'the commit changes paths needing {", ".join(missing)}, which the scoped receipt command does not name')
    return found


# ---- ranked gate lease (in-ledger slots table) ----------------------------------------------------------------

LEASE_SCHEMA = ("CREATE TABLE IF NOT EXISTS gate_tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,"
                " rank INTEGER NOT NULL, holder TEXT NOT NULL, slot INTEGER, requested REAL NOT NULL,"
                " heartbeat REAL NOT NULL)")
DEFAULT_SLOTS, DEFAULT_RESERVED, DEFAULT_RESERVED_RANK, DEFAULT_TTL = 5, 1, 2, 60.0
MAX_SLOTS = 32
AGING_S = 1800.0   # a waiting ticket gains one effective rank per 30 minutes, so P4 work cannot starve


def rank_for(priority):
    """Task priority P0..P4 -> rank 1..5. A lower rank is served first."""
    return PRIORITIES.index(priority) + 1


def _tx(conn):
    from .ledger.db import tx
    return tx(conn)


def lease_limits(slots, reserved):
    """(slots, reserved) from raw hub-setting values: slots clamped to 1..MAX_SLOTS, reserved to 0..slots-1.
    A value that is not an integer raises ValueError (the caller falls back to the default)."""
    slots = max(1, min(MAX_SLOTS, int(str(slots).strip())))
    return slots, max(0, min(int(str(reserved).strip()), slots - 1))


_BAD_LIMITS = {}   # setting key -> last invalid raw value logged


def configured_limits(get_setting):
    """(slots, reserved) from the hub settings `gate_slots`/`gate_reserved`, read through get_setting(key, default).
    Missing or invalid values use the defaults (an invalid one logs once per value); the result is clamped."""
    out = []
    for key, default in (("gate_slots", DEFAULT_SLOTS), ("gate_reserved", DEFAULT_RESERVED)):
        raw = get_setting(key, None)
        try:
            out.append(default if raw in (None, "") else int(str(raw).strip()))
        except ValueError:
            if _BAD_LIMITS.get(key) != raw:
                _BAD_LIMITS[key] = raw
                print(f"drover.gate: ignoring {key}={raw!r}: not an integer, using {default}", file=sys.stderr)
            out.append(default)
    return lease_limits(*out)


def lease_request(conn, task_id, priority, holder='gate', at=None):
    """Queue a ticket; returns its id. Order is rank, then ticket id (FIFO)."""
    at = time.time() if at is None else at
    conn.execute(LEASE_SCHEMA)
    with _tx(conn):
        return conn.execute("INSERT INTO gate_tickets(task_id,rank,holder,requested,heartbeat) VALUES(?,?,?,?,?)",
                            (task_id, rank_for(priority), holder, at, at)).lastrowid


def lease_acquire(conn, ticket, slots=DEFAULT_SLOTS, reserved=DEFAULT_RESERVED, reserved_rank=DEFAULT_RESERVED_RANK,
                  ttl=DEFAULT_TTL, at=None):
    """The ticket's slot number once it is the best waiting ticket that a free slot can serve, else None.

    The last `reserved` slots serve only tickets of rank <= reserved_rank (their own rank: aging never grants the reserve).
    Waiting gains one effective rank per 30 minutes. Tickets that stop heartbeating for
    `ttl` seconds are dropped, so a crashed gate frees its slot.
    """
    at = time.time() if at is None else at
    reserved = max(0, min(reserved, slots - 1))
    with _tx(conn):
        conn.execute("DELETE FROM gate_tickets WHERE heartbeat < ?", (at - ttl,))
        mine = conn.execute("SELECT * FROM gate_tickets WHERE id=?", (ticket,)).fetchone()
        if mine is None:
            raise GateError(f'ticket {ticket} expired or was cancelled')
        conn.execute("UPDATE gate_tickets SET heartbeat=? WHERE id=?", (at, ticket))
        if mine['slot'] is not None:
            return mine['slot']
        held = {r['slot'] for r in conn.execute("SELECT slot FROM gate_tickets WHERE slot IS NOT NULL")}
        free = [s for s in range(slots) if s not in held]
        general = slots - reserved
        waiting = conn.execute("SELECT id, rank, requested FROM gate_tickets WHERE slot IS NULL").fetchall()
        waiting.sort(key=lambda t: (t['rank'] - int(max(0.0, at - t['requested']) // AGING_S), t['id']))
        for t in waiting:
            eligible = [s for s in free if s < general or t['rank'] <= reserved_rank]
            if not eligible:
                continue
            pick = eligible[-1] if t['rank'] <= reserved_rank else eligible[0]  # top ranks use the reserve first
            free.remove(pick)
            if t['id'] == ticket:
                conn.execute("UPDATE gate_tickets SET slot=? WHERE id=?", (pick, ticket))
                return pick
    return None


def lease_heartbeat(conn, ticket, at=None):
    conn.execute("UPDATE gate_tickets SET heartbeat=? WHERE id=?", (time.time() if at is None else at, ticket))


def lease_release(conn, ticket):
    """Release or cancel a ticket; safe to call twice."""
    conn.execute("DELETE FROM gate_tickets WHERE id=?", (ticket,))


def lease_queue(conn):
    conn.execute(LEASE_SCHEMA)
    return [dict(r) for r in conn.execute("SELECT * FROM gate_tickets ORDER BY rank, id")]


def run_leased(db_path, task_id, priority, fn, *, slots=None, reserved=None,
               reserved_rank=DEFAULT_RESERVED_RANK, ttl=DEFAULT_TTL, poll=0.25, timeout=None, holder='gate'):
    """Wait for a slot in the ledger at `db_path` (slots/reserved default to the hub settings), call fn(slot) while a heartbeat thread keeps the lease, release."""
    from .ledger.db import connect
    conn = connect(db_path)
    if slots is None or reserved is None:       # the owner's gate_slots/gate_reserved, shared with the dispatcher
        from .ledger import ops
        cfg = configured_limits(lambda k, d: ops.get_setting(conn, k, d))
        slots, reserved = cfg[0] if slots is None else slots, cfg[1] if reserved is None else reserved
    ticket = lease_request(conn, task_id, priority, holder)
    stop, thread = threading.Event(), None
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        slot = lease_acquire(conn, ticket, slots, reserved, reserved_rank, ttl)
        while slot is None:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f'no gate slot for {task_id} within {timeout}s')
            time.sleep(poll)
            slot = lease_acquire(conn, ticket, slots, reserved, reserved_rank, ttl)

        def beat():
            side = connect(db_path, migrate_db=False)
            while not stop.wait(ttl / 3):
                try:
                    lease_heartbeat(side, ticket)
                except sqlite3.Error:
                    pass
            side.close()
        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        return fn(slot)
    finally:
        stop.set()
        if thread:
            thread.join(5)
        try:
            lease_release(conn, ticket)
        finally:
            conn.close()


# ---- CLI ---------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m drover.gate', description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='action', required=True)
    r = sub.add_parser('run')
    r.add_argument('--out', required=True)
    r.add_argument('--log')
    r.add_argument('--repo', default='.')
    r.add_argument('--repo-name', help='repo configuration supplying gate scope and project pattern')
    r.add_argument('--base', default='origin/main')
    r.add_argument('--pin', action='append', default=[])
    r.add_argument('--priority', choices=PRIORITIES, default='P2')
    r.add_argument('--memory-max', default='8G')
    r.add_argument('--max-seconds', type=int, default=1800)
    r.add_argument('--auto-scope', metavar='MAP.json')
    r.add_argument('--expect-runs', type=int, default=0)
    r.add_argument('--retry-failed', type=int, default=0)
    r.add_argument('--retry-command')
    r.add_argument('--failure-pattern', default=DEFAULT_FAILURE_PATTERN)
    r.add_argument('--json', action='store_true')
    r.add_argument('command', nargs=argparse.REMAINDER)
    v = sub.add_parser('verify')
    v.add_argument('--receipt', required=True)
    v.add_argument('--repo', default='.')
    v.add_argument('--base', default='origin/main')
    s = sub.add_parser('scope')
    s.add_argument('--repo', default='.')
    s.add_argument('--base', default='origin/main')
    s.add_argument('--map')
    s.add_argument('--json', action='store_true')
    p = sub.add_parser('parse')
    p.add_argument('log')
    p.add_argument('--diff', action='append', default=[])
    p.add_argument('--json', action='store_true')
    a = parser.parse_args(argv)
    try:
        return _dispatch(a)
    except GateError as e:
        print(f'drover.gate: {e}', file=sys.stderr)
        return 2


def _dispatch(a):
    if a.action == 'run':
        project_pattern = ''
        if a.repo_name:
            from . import repos
            configured = repos.load_all().get(a.repo_name.lower())
            if configured is None:
                raise GateError(f'no repo config for {a.repo_name!r}')
            project_pattern = configured['gate']['project_pattern']
            a.auto_scope = a.auto_scope or repos.scope_map_path(configured)
        command = a.command[1:] if a.command[:1] == ['--'] else a.command
        rec = run_gate(a.repo, a.out, command, base=a.base, pins=a.pin, priority=a.priority, memory_max=a.memory_max,
                       max_seconds=a.max_seconds, auto_scope=a.auto_scope, expect_runs=a.expect_runs,
                       retry_failed=a.retry_failed, retry_command=a.retry_command,
                       failure_pattern=a.failure_pattern, project_pattern=project_pattern, log=a.log)
        print(json.dumps(rec) if a.json else
              f'receipt {a.out}: sha {rec["sha"][:12]} exit {rec["exit_code"]} {rec["status"].upper().replace("_", " ")}')
        return 0 if rec['status'] != 'failed' else 1
    if a.action == 'verify':
        receipt = json.loads(Path(a.receipt).read_text())
        found = problems(receipt, Path(a.repo).resolve(), a.base)
        for text in found:
            print('refused: ' + text)
        if not found:
            print(f'receipt verified: {receipt["sha"][:12]} passed {" ".join(receipt["command"])}')
        return 1 if found else 0
    if a.action == 'scope':
        result = scope_for(a.repo, a.base, a.map)
        print(json.dumps(result, indent=2) if a.json else '\n'.join(result['projects']))
        return 0
    failures = parse_failures(Path(a.log).read_text(errors='replace'), a.diff)
    if a.json:
        print(json.dumps(failures))
    else:
        for f in failures:
            print(f'{f["test"]}\t{f["file"] or "-"}\t{"in diff" if f["in_diff"] else "not in diff"}\t{f["error"]}')
    return 0 if failures else 1


if __name__ == '__main__':
    sys.exit(main())
