"""Task kinds and declarative repository read access, shared by all input paths."""
import json
import re
import subprocess
from pathlib import Path

from . import repos

KINDS = ('research', 'investigation', 'implementation', 'fix', 'review', 'docs', 'mechanical')
READ_ONLY_KINDS = ('research', 'investigation', 'review')

# These are task authority, not suggestions to be deferred to a skill approval gate.
KEEP_GOING = (
    'Keep going until every acceptance criterion is implemented and verified with targeted checks. '
    'Fix code and test failures and continue; the Drover gate handles the full platform suites. '
    'Report ready_to_commit (or done) only after that verification. '
    'For read-only work, verify and report the findings without editing files.'
)
WORKER_DECISIONS = (
    'The approved task, scope and acceptance criteria are the approved design. '
    'Use existing repository conventions and the smallest reversible implementation that satisfies them. '
    'Choose routine implementation details within that authority and record your choice; '
    'do not wait for skill approval gates. Apply recorded decisions as firm choices and continue '
    'instead of reporting the same resolved blocker again. Never expand scope or override repository safety rules. '
    'Only the owner can authorize destructive actions, new access, dependencies, publishing, '
    'or a change to the approved goal or acceptance criteria.'
)
CONTINUE_SETTING = 'worker_continue_attempts'  # ledger setting; zero disables automatic continuation
CONTINUE_ATTEMPTS = 3


def worker_instructions():
    return '## Firm decisions\n' + WORKER_DECISIONS + '\n\n' + KEEP_GOING


def continuation_choices(blocker):
    """Give the choice model actionable alternatives when the worker asks A or B.

    The systemone client returns a choice label, not generated prose. Put the actual
    resolutions in its criteria so both the durable event and resumed prompt carry
    the selected implementation, rather than another instruction to make a choice.
    """
    detail = str(blocker.get('detail') or '').strip()
    match = re.search(
        r'\b(use|choose|pick|select|adopt|implement|be)\s+(.+?)\s+(?:or|versus|vs\.?)\s+([^?\n]+)',
        detail, re.IGNORECASE)
    if match:
        verb, left, right = match.groups()
        verb = 'choose' if verb.lower() == 'be' else verb
    else:
        match = re.fullmatch(r'(?:[Ww]hether\s+)?(`[^`]+`|[\w./+-]+)\s+(?:or|versus|vs\.?)\s+(`[^`]+`|[\w./+-]+)\??', detail)
        if not match:
            return dict(CONTINUE_DECISIONS)
        verb, (left, right) = 'choose', match.groups()
    options = [left.strip(' .'), right.rstrip(' .?')]
    return {f'option_{i}': f'{verb.capitalize()} {option}.'
            for i, option in enumerate(options, 1)}


def continuation_criteria(choices=None):
    """An inline criteria file lets this bounded decision use the existing decider client."""
    from . import decide
    return {'version': 1, 'defaults': decide.load_criteria()['defaults'], 'questions': {
        'worker_continue': {
            'type': 'choice', 'mode': 'live', 'criteria_version': 1,
            'instructions': (
                'Decide how to resolve this worker stop using the task, repository rules, owner notes '
                'and recorded decisions. Do not treat a request for skill approval, implementation '
                'uncertainty, missing tests or a sandbox limitation as owner authority. '
                'Select owner only for a specific choice reserved to the owner or contradictory acceptance. '
                'Select only alternatives authorized by the task and repository rules. Prefer the existing '
                'conventions and honor recorded decisions. The selected continuation is a firm '
                'decision the same worker must implement.'),
            'criteria': {**(choices or CONTINUE_DECISIONS),
                'owner': 'A specific unresolved choice requires owner authority or changes the approved acceptance.'}}}}


CONTINUE_DECISIONS = {
    'follow_spec': 'Implement the approved goal and acceptance exactly. The task is the approved design; proceed without another approval gate.',
    'minimal_change': 'Follow existing repository patterns. Choose the smallest reversible implementation within the approved scope.',
    'verify': 'Finish the implementation, fix failures and run targeted checks for every acceptance criterion.'}


def owner_choice(blocker):
    """Explicit authority boundaries remain binding even when the model suggests continuing."""
    kind = str(blocker.get('kind') or '').lower().replace('-', '_')
    return kind in {'owner', 'owner_only', 'owner_decision', 'destructive', 'new_access',
                    'dependency', 'new_dependency', 'publish', 'scope_change', 'contradictory_acceptance'}


def owner_question(blocker, exhausted=False):
    detail = str(blocker.get('detail') or 'the remaining acceptance criteria').strip()
    if exhausted:
        return 'Continuation budget exhausted: how should the worker resolve ' + detail.rstrip('.?') + '?'
    return detail if detail.endswith('?') else 'How should the worker resolve ' + detail.rstrip('.') + '?'


def _read_repository_rules(cwd, scopes):
    root = Path(cwd).resolve()
    directories = {root}
    for scope in scopes:
        path = (root / scope.split(':', 1)[0]).resolve()
        if path.is_relative_to(root):
            directories.update(p for p in (path, *path.parents) if p.is_relative_to(root))
    rules = {}
    for directory in sorted(directories):
        for name in ('AGENTS.md', 'CLAUDE.md'):
            path = directory / name
            try:
                rules[str(path.relative_to(root))] = path.read_text(encoding='utf-8')
            except (FileNotFoundError, NotADirectoryError):
                pass
    return rules


def repository_rules(task, cwd):
    """Read root and scoped rules through the worktree's placement; fail closed on IO errors."""
    if not cwd:
        return {}
    from . import placement
    scopes = task.get('scope_files') or []
    if not placement.routes_through_wsl(cwd):
        return _read_repository_rules(placement.native(cwd), scopes)
    # Ship the fixed reader itself, so native and remote reads have identical path
    # containment and error handling. No Drover installation is needed in the worktree.
    import inspect
    code = ('import json, sys\nfrom pathlib import Path\n' +
            inspect.getsource(_read_repository_rules) +
            '\nprint(json.dumps(_read_repository_rules(sys.argv[1], json.load(sys.stdin))))')
    argv = placement.WSL().command(['python3', '-c', code, str(cwd)])
    result = subprocess.run(argv, input=json.dumps(scopes), capture_output=True,
                            text=True, encoding='utf-8', timeout=15, **placement.NO_WINDOW)
    if result.returncode:
        raise OSError(result.stderr.strip() or 'cannot read repository rules')
    return json.loads(result.stdout)


def check_kind(kind):
    if kind not in KINDS:
        raise ValueError(f'kind must be one of {", ".join(KINDS)}')
    return kind


def split_read(entry):
    if not isinstance(entry, str) or not entry or entry.count('@') > 1:
        raise ValueError('read_access entries must be repo or repo@branch')
    name, sep, branch = entry.partition('@')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', name):
        raise ValueError(f'invalid read repo {name!r}')
    if sep and (not branch or branch.startswith(('-', '/')) or branch.endswith(('.', '/'))
                or '..' in branch or '//' in branch or '@{' in branch
                or re.search(r'[\s\x00-\x1f\x7f~^:?*\[\\]', branch)
                or any(p.startswith('.') or p.endswith('.lock') for p in branch.split('/'))):
        raise ValueError(f'invalid read branch {branch!r}')
    return name, branch or None


def read_access(entries, repo, configured=None):
    configured = repos.load_all() if configured is None else configured
    if entries is None:
        default = configured.get(str(repo).lower(), {}).get('default_branch', 'main')
        return [f'{repo}@{default}']
    if not isinstance(entries, (list, tuple)):
        raise ValueError('read_access must be a list')
    out = []
    for entry in entries:
        name, _ = split_read(entry)
        if name.lower() not in configured:
            raise ValueError(f'unknown read repo {name!r}')
        if entry not in out:
            out.append(entry)
    return out
