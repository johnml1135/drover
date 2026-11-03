"""Every rule that says whether a path may go, with no mutation: pure selection and grace transitions, the one place
quarantine state is stored, and the disposable-path rules. A rule answers with a Ruling and never raises to mean
"no"; a read that fails still raises, and the stage keeps the row."""
import copy
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from . import placement
from .disk_evidence import FreshProcesses, FreshRegistrations, overlaps
from .disk_inventory import repository_root, safe_path, worktree_roots
from .disk_stage import within

OUTPUT_IDLE_S = 2 * 3600  # disk_cleanup.worktree_output_idle_s default


@dataclass(frozen=True)
class Ruling:
    action: Literal['candidate', 'keep', 'report']
    reason_code: str
    detail: str | None = None
    facts: Mapping | None = None


def decide(request, observations, previous_state, now):
    """Pure selection and grace transitions; candidates are never deletion authority.

    Observations contain a report row and optional settlement or quarantine facts.
    Evidence adapters own path/process/Git probes. Only their apply method may
    mutate a checkout, after obtaining fresh evidence and durably journaling it.
    Checks are semantic retention facts; settlement supplies task/ancestry facts.
    Quarantine supplies a canonical key and activity identity. approved_match is
    canonical path equality computed by the adapter, without path probes here.
    Validation supplies fresh identity, nesting and ignored-content facts.
    Neither observations nor previous_state are changed by this function.
    """
    state = copy.deepcopy(previous_state)
    events, decisions = [], []
    for observed in observations:
        row = copy.deepcopy(observed['row'])
        checks = observed.get('checks', {})
        blocked = next((reason for failed, reason in (
            (checks.get('other_side'), 'other_side_or_lock_note'),
            (checks.get('other_drive'), 'other_drive'),
            (checks.get('main'), 'main_checkout'),
            (checks.get('busy'), checks.get('process_reason') or 'busy'),
            (checks.get('protected'), 'protected'),
            (checks.get('missing'), 'missing'),
            (checks.get('live'), 'busy'),
            (bool(row.get('dirty')), 'dirty')) if failed), None)
        if blocked:
            row.update(action='candidate' if blocked == 'missing' else 'keep', reason_code=blocked)
        settlement = observed.get('settlement') if not blocked else None
        if settlement is not None:
            tag = row.get('tag')
            live, terminal = settlement['live'], settlement['terminal']
            lease = bool(tag and tag['v'] == 2 and tag['repo'] == row['name']
                         and tag['lease_until'] > now)
            if live:
                row.update(action='keep', reason_code='busy')
            elif not lease:
                row.update(action='keep', reason_code='orphan')
            elif terminal or settlement['merged']:
                row.update(action='candidate', reason_code='settled')
        quarantine = observed.get('quarantine')
        if quarantine is not None:
            key = quarantine['key']
            if quarantine.get('live'):
                row.update(action='keep', reason_code='live_owner')
            eligible = row['reason_code'] == 'orphan'
            if eligible and quarantine.get('excluded'):
                row.update(reason_code='ignored_content', excluded=quarantine['excluded'])
                eligible = False
            if request['apply']:
                records = state.setdefault('quarantine', {})
                previous = records.get(key)
                fingerprint = quarantine.get('identity')
                if not eligible:
                    if records.pop(key, None) is not None:
                        events.append(dict(kind='quarantine_reset', path=row['path']))
                elif not previous or previous['identity'] != fingerprint:
                    records[key] = dict(identity=fingerprint, since=now)
                    events.append(dict(kind='quarantine_started', path=row['path']))
                elif now - previous['since'] >= 3 * 86400:
                    row.update(action='candidate', reason_code='quarantine_elapsed',
                               quarantine_identity=fingerprint)
        approved = request.get('approved_path')
        if approved:
            if not observed.get('approved_match', row['path'] == approved):
                continue
            if row['reason_code'] in ('unsettled', 'orphan'):
                row['action'] = 'candidate'
        validation = observed.get('validation', {})
        if validation.get('identity_matches') is False:
            row.update(action='keep', reason_code='revalidation_changed')
        elif validation.get('nested'):
            row.update(action='keep', reason_code='nested_worktree')
        elif validation.get('excluded'):
            row.update(action='keep', reason_code='ignored_content', excluded=validation['excluded'])
        decisions.append(row)
    return dict(decisions=decisions, state=state, events=events)



def decide_and_store(request, observations, store):
    def update(state):
        answer = decide(request, observations, state, request['now'])
        state.clear()
        state.update(answer['state'])
        return answer
    # Reports must not even write an unchanged state file.
    return store.update(update) if request['apply'] else decide(request, observations, {}, request['now'])


@dataclass(frozen=True)
class DisposableUnit:
    """One unit of declared disposable paths as the first pass found it: first-pass values only. `registration` is
    the listing row, `evidence` the (path, head, tag, declarations) `rule_disposable` gave, `measured` name -> facts."""
    machine: Mapping
    registration: Mapping
    evidence: tuple
    touched: set
    target: Path
    measured: Mapping
    row: dict


def repository_tree(path, *, boundary=None, proven=None):
    """Repository evidence protects even damaged, unregistered and bare trees.

    A worktree's own gitfile is allowed only at the output stage's checked boundary.
    Never follow links while looking for evidence; unreadable trees fail closed. `proven` is as for safe_path.
    """
    path = Path(path).absolute()
    checked = []
    for parent in (path, *path.parents):
        if proven is not None and (parent, boundary) in proven:
            break
        if boundary is not None and placement.cleanup_parts(parent) == placement.cleanup_parts(boundary):
            break
        if not safe_path(parent, proven) or repository_root(parent):
            return True
        checked.append((parent, boundary))
    if proven is not None:
        proven.update(key for key in checked if key[0] != path)
    if path.is_dir():
        for child in path.iterdir():
            if not safe_path(child) or repository_root(child) or child.is_dir() and repository_tree(child, boundary=path):
                return True
    return False


def live_tree(path, run):
    """Fresh live paths protect both containing trees and their descendants, as the stage's evidence knows them."""
    evidence = run.evidence
    snapshot = evidence.fresh_snapshot() or {}      # a stale snapshot makes no path live
    owners = list(snapshot.get('active_worktrees', []))
    owners += [t['worktree'] for t in snapshot.get('tasks', []) if t.get('worktree')]
    parts = placement.cleanup_parts(path)
    for owner in owners:
        other = placement.cleanup_parts(owner)
        if (parts[:len(other)] == other or other[:len(parts)] == parts) and \
                evidence.task_facts(Path(placement.native(owner)), None)[0]:
            return True
    return False


def protected_path(path, run):
    request = run.request
    path = Path(path).absolute()
    roots = [Path(request.state_dir).absolute()]
    roots += [Path(placement.native(repo['repo'])).absolute() for repo in request.repos if repo.get('repo')]
    roots += [root.absolute() for repo in request.repos for root in worktree_roots(repo)]
    return not placement.cleanup_contains(request.drive['path'], str(path)) or live_tree(path, run) or not safe_path(path) or any(within(path, root) or within(root, path) for root in roots) or \
        repository_tree(path)


def disposable_match(relative, declarations):
    """Match a path or any containing directory against committed relative globs."""
    parts = Path(relative).parts
    def match(names, pattern):
        if not pattern:
            return not names
        if pattern[0] == '**':
            return match(names, pattern[1:]) or bool(names) and match(names[1:], pattern)
        return bool(names) and fnmatch.fnmatchcase(names[0], pattern[0]) and match(names[1:], pattern[1:])
    return any(match(parts[:end], tuple(pattern.split('/')))
               for pattern in declarations for end in range(1, len(parts) + 1))


def ignored_outside(path, ignored, declarations):
    return [name for name in ignored if repository_tree(Path(name), boundary=path) or
            not disposable_match(Path(name).relative_to(path), declarations)]


def _unavailable(detail):
    """The keep every refused disposable path carries; `detail` is the row's `error`, as the decision trace reads it."""
    return Ruling('keep', 'output_evidence_unavailable', detail)


def _disposable(run, machine, registration, proof, touched):
    """`output_evidence` as it was, each raise now a keep; `proof()` is the process proof, asked only past the path
    checks, as it was."""
    request, evidence = run.request, run.evidence
    path = Path(placement.native(registration['worktree']))
    def reject(condition, detail):
        return _unavailable(f'not an eligible owning-side worktree: {condition} ({detail})')
    if placement.cleanup_parts(path) == placement.cleanup_parts(machine['repo']):
        return reject('main_checkout', f'repo={machine["repo"]!r}')
    if not path.is_dir():
        return reject('not_directory', f'path={str(path)!r}')
    if not safe_path(path):
        return reject('unsafe_path', f'path={str(path)!r}')
    side = placement.worktree_side(registration['worktree'], registration.get('locked'))
    if side != placement.cleanup_side():
        return reject('owner_side', f'owner={side!r}, worker={placement.cleanup_side()!r}, lock={registration.get("locked")!r}')
    if not placement.cleanup_contains(request.drive['path'], registration['worktree']):
        return reject('physical_drive', f'path_side={placement.cleanup_path_side(registration["worktree"])!r}, '
                      f'host={placement.physical_disk_path(registration["worktree"])!r}, '
                      f'drive={request.drive["path"]!r}, '
                      f'drive_host={placement.physical_disk_path(request.drive["path"])!r}')
    if proof().busy(str(path)):
        return _unavailable('process using worktree')
    # This worker cannot see Windows processes; a budget carries what the coordinator found them naming inside WSL.
    if any(within(named, path) for named in (request.budget or {}).get('windows_paths') or ()):
        return _unavailable('windows process using worktree')
    # Disposable paths only: whole-worktree removal keeps its own three-day settlement (disk_cleanup_decisions).
    # A drive in trouble waives the idle rule; a budget purge, which is about WSL and not that drive, never does.
    limit = (request.policy or {}).get('worktree_output_idle_s', OUTPUT_IDLE_S)
    if (request.budget or request.drive.get('level') not in ('hold', 'critical')) and evidence.measure(path, ignore_activity=touched)['age_s'] < limit:
        return _unavailable(f'worktree idle less than {limit:g} seconds')
    head = evidence.head(path)
    return Ruling('candidate', 'declared_disposable', facts=dict(
        path=path, head=head, tag=evidence.tag(path), declarations=evidence.declarations(machine, path)))


def rule_disposable(run, machine, registration, *, touched=()) -> Ruling:
    """First pass: whether a listed worktree's outputs may be judged, on the stage's first-pass process proof. A
    candidate's `facts` are its (path, head, tag, declarations)."""
    return _disposable(run, machine, registration, lambda: run.evidence.processes(), touched)


def rule_disposable_fresh(run, machine, registration, processes: FreshProcesses, *, touched=()) -> Ruling:
    """A removal's last look: `rule_disposable` on a process proof taken now, which only `fresh_processes` makes."""
    if not isinstance(processes, FreshProcesses):
        raise TypeError('rule_disposable_fresh takes a FreshProcesses, taken now')
    return _disposable(run, machine, registration, lambda: processes.proof, touched)


def rule_unit(run, target, path, names) -> Ruling:
    """First pass: whether one unit (`target`, its listed `names`) of the worktree at `path` is a candidate. Its
    `facts` hold `measured`, name -> facts, and `protected`, the leaves of a several-name unit left out as protected,
    each a row the caller adds, even when a read fails partway, which keeps the unit with that read's error."""
    request, stage = run.request, run.evidence
    if not placement.cleanup_contains(request.drive['path'], str(target)):
        return Ruling('keep', 'drive_unproven')
    if live_tree(target, run):
        return Ruling('keep', 'live_owner')
    if overlaps(target, path, stage.every_registered()):    # a failed repo raises: each unit stays
        return Ruling('keep', 'protected')
    proven, measured, protected = set(), {}, []
    try:
        for name in names:
            leaf = Path(name)
            if safe_path(leaf, proven) and not (repository_tree(leaf, boundary=path, proven=proven) if request.apply else stage.inventory.contains_repository(leaf)):
                measured[name] = stage.measure(leaf)
            elif len(names) > 1:
                protected.append(leaf)
    except (OSError, ValueError, StopIteration) as error:
        return Ruling('keep', 'output_evidence_unavailable', str(error), dict(measured={}, protected=tuple(protected)))
    facts = dict(measured=measured, protected=tuple(protected))
    return Ruling('candidate', 'declared_disposable', facts=facts) if measured else Ruling('keep', 'protected', facts=facts)


def rule_unit_fresh(run, unit: DisposableUnit, proof: FreshRegistrations) -> Ruling:
    """A unit's last look before its paths go, on the registrations proven now: any refusal, any overlap, repository
    evidence, the drive or a live owner keeps it."""
    if not isinstance(proof, FreshRegistrations):
        raise TypeError('rule_unit_fresh takes a FreshRegistrations, proven now')
    if proof.refused:
        first = proof.refused[0]
        return Ruling('keep', 'registration_unknown' if first.why == 'unknown' else 'output_evidence_unavailable',
                      f'disposable registration changed ({first.detail})')
    path, proven = unit.evidence[0], set()
    if proof.overlaps or \
            any(repository_tree(Path(name), boundary=path, proven=proven) for name in unit.measured) or \
            not placement.cleanup_contains(run.request.drive['path'], str(unit.target)) or live_tree(unit.target, run):
        return _unavailable('disposable side marker changed')
    return Ruling('candidate', 'declared_disposable')

