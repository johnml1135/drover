"""Every rule that says whether a path may go, with no mutation: pure selection and grace transitions, the one place
quarantine state is stored, the worktree check order, and the disposable, temp, build-root and cache rules. A rule answers with a
Ruling and never raises to mean "no"; a read that fails still raises, and the stage keeps the row."""
import copy
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from . import disk_evidence, placement
from .disk_evidence import FreshProcesses, FreshRegistrations, overlaps
from .disk_inventory import repository_root, safe_path, worktree_roots
from .disk_stage import item, within
from .disk_state import CleanupState

OUTPUT_IDLE_S = 2 * 3600  # disk_cleanup.worktree_output_idle_s default
CLOSED_IDLE_S = 3 * 86400
OUTPUT_UNAVAILABLE = 'output_evidence_unavailable'      # every disposable path kept for want of evidence


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


def _view(run):
    """What `decide` and `decide_and_store` read of the stage's request. An approval is the caller's, never theirs."""
    return dict(apply=run.request.apply, now=run.request.now, approved_path=None)


class LazyProof:
    """A process proof taken when `inspect` first reads it, past the path checks, so a row those checks keep takes
    none, as before the move. `take` gives the proof: the stage's first-pass one, or a removal's fresh one."""
    def __init__(self, take):
        self._take, self._proof = take, None

    def _taken(self):
        if self._proof is None:
            self._proof = self._take()
        return self._proof

    @property
    def busy(self):
        return self._taken().busy

    @property
    def reason(self):       # a proof without one raises AttributeError, so `getattr`'s default reads it as before
        return self._taken().reason


def process_reason(processes):
    """Why a proof finds everything busy, or None: a proof without a `reason`, as a test's may be, gives none."""
    return getattr(processes, 'reason', None)


def needs_idle_gate(run, tag):
    """True when the tag's task is closed without landing (yanked, abandoned, handed back): its tree may hold
    undecided work, so it goes only once quiet for three days. Anything unclear is not a reason to hurry."""
    snapshot = run.evidence.fresh_snapshot()
    if snapshot is None or not tag:
        return False
    return any(tag['owner'] == 'drover:' + str(t.get('id')) and t.get('state') != 'landed'
               for t in snapshot.get('tasks', []))


def inspect(run, machine, registration, *, processes) -> dict:
    """One listed worktree judged on the stage's evidence. `processes` is the first pass's process proof, or the fresh
    one a removal's last look takes; a LazyProof is taken only once the path checks pass."""
    request, evidence = run.request, run.evidence
    path = Path(placement.native(registration['worktree']))
    row = item(path, 'unsettled', repo=machine['repo'], name=machine['name'],
               head=registration.get('HEAD'), branch=registration.get('branch'), side_lock=registration.get('locked'))
    checks = {}
    observed = dict(row=row, checks=checks)
    def decision():
        return decide(_view(run), [observed], {}, request.now)['decisions'][0]
    checks['other_side'] = placement.worktree_side(registration['worktree'], registration.get('locked')) != placement.cleanup_side()
    if checks['other_side']:
        return decision()
    checks['other_drive'] = not placement.cleanup_contains(request.drive['path'], registration['worktree'])
    if checks['other_drive']:
        return decision()
    checks['main'] = placement.cleanup_parts(path) == placement.cleanup_parts(machine['repo'])
    if checks['main']:
        return decision()
    busy = processes.busy       # a LazyProof is taken here, before the task facts, where the proof always was
    checks.update(busy=evidence.task_facts(path, None)[0] or busy(str(path)),
                  process_reason=process_reason(processes))
    if checks['busy']:
        return decision()
    checks['protected'] = not safe_path(path)
    if checks['protected']:
        return decision()
    checks['missing'] = not path.exists()
    if checks['missing']:
        return decision()
    tag = evidence.tag(path)
    row.update(tag=tag, owner=tag['owner'] if tag else None)
    live, terminal = evidence.task_facts(path, tag)
    checks['live'] = live
    if live:
        return decision()
    dirty, ignored = evidence.status(path, expand=request.apply)
    row.update(dirty=dirty, ignored=ignored)
    if dirty:
        return decision()
    head = evidence.head(path)
    row.update(head=head, gitdir=evidence.gitdir(path))
    row.update(evidence.measure(path))
    lease = bool(tag and tag['v'] == 2 and tag['repo'] == machine['name'] and tag['lease_until'] > request.now)
    # A task closed without landing may still hold undecided work: three quiet days first; the branch always stays.
    if terminal and needs_idle_gate(run, tag) and row['age_s'] < CLOSED_IDLE_S:
        terminal = False
    observed['settlement'] = dict(live=live, terminal=terminal,
                                 merged=evidence.merged(machine, head) if lease and not terminal else False)
    return decision()


def unlisted_live(run, path):
    tag = run.evidence.tag(path)
    return run.evidence.task_facts(path, tag)[0] or bool(tag and tag['v'] == 2 and tag['lease_until'] > run.request.now)


def quarantine_observation(run, machine, row):
    key = repr((placement.cleanup_parts(machine['repo']), placement.cleanup_parts(row['path'])))
    live = bool(row.get('unlisted') and unlisted_live(run, Path(row['path'])))
    eligible = row['reason_code'] == 'orphan' and not live
    excluded = ignored_outside(Path(row['path']), row['ignored'],
                              run.evidence.declarations(machine, machine['repo'] if row.get('unlisted') else row['path'])) if eligible else []
    fingerprint = (disk_evidence.identity(Path(row['path'])) + repr((row.get('head'), row.get('tag'),
                    row.get('branch'), placement.cleanup_parts(row['gitdir']) if row.get('gitdir') else None, row.get('side_lock')))) if eligible and not excluded else None
    return dict(row=row, quarantine=dict(key=key, identity=fingerprint, excluded=excluded, live=live))


def quarantine(run, machine, row, *, store=None):
    observation = quarantine_observation(run, machine, row)
    answer = decide_and_store(_view(run), [observation],
                              store if store is not None else CleanupState(run.request.state_dir))
    return answer['decisions'][0]


@dataclass(frozen=True)
class DisposableUnit:
    """One unit of declared disposable paths as the first pass found it: first-pass values only. `registration` is
    the listing row, `evidence` the (path, head, tag, declarations) `rule_disposable` gave, `measured` name -> facts
    (a folder `disposable_status` listed whole is one name), `untracked` the measured names listed `??`, not `!!`."""
    machine: Mapping
    registration: Mapping
    evidence: tuple
    touched: set
    target: Path
    measured: Mapping
    row: dict
    untracked: frozenset = frozenset()


def disposable_status(run, path, declarations):
    """(dirty, ignored) for stage 5's units at `path`, read the same way on the first pass and at removal, dry run and
    apply alike: as git reports them, so a folder it reports whole (nothing under it tracked) is one name. A listed
    folder not itself declared, where a declaration could match below it, is replaced by every path under it, from one
    expanded status; other folders stay whole. A budget purge, whose units are declared prefixes, lists every path."""
    expand = bool(run.request.budget)
    dirty, ignored = run.evidence.status(path, expand=expand)
    if expand or not declarations:
        return dirty, ignored
    opened = {Path(name) for name in dirty + ignored if Path(name).is_dir() and
              not disposable_match(Path(name).relative_to(path), declarations) and
              open_below(Path(name).relative_to(path), declarations)}
    if not opened:
        return dirty, ignored
    every = run.evidence.status(path)
    return tuple([name for name in listed if Path(name) not in opened] +
                 [name for name in all_names if any(f == Path(name) or f in Path(name).parents for f in opened)]
                 for listed, all_names in zip((dirty, ignored), every))


def open_below(relative, declarations):
    """Whether a declaration could match a path strictly below the folder `relative` (`**` takes any parts)."""
    def below(names, pattern):
        if not names or not pattern:
            return bool(pattern) and not names
        if pattern[0] == '**':
            return below(names, pattern[1:]) or below(names[1:], pattern)
        return fnmatch.fnmatchcase(names[0], pattern[0]) and below(names[1:], pattern[1:])
    return any(below(Path(relative).parts, tuple(pattern.split('/'))) for pattern in declarations)


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
    return Ruling('keep', OUTPUT_UNAVAILABLE, detail)


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
        return Ruling('keep', OUTPUT_UNAVAILABLE, str(error), dict(measured={}, protected=tuple(protected)))
    facts = dict(measured=measured, protected=tuple(protected))
    return Ruling('candidate', 'declared_disposable', facts=facts) if measured else Ruling('keep', 'protected', facts=facts)


def rule_unit_fresh(run, unit: DisposableUnit, proof: FreshRegistrations) -> Ruling:
    """A unit's last look before its paths go, on the registrations proven now: any refusal, any overlap, repository
    evidence, the drive or a live owner keeps it."""
    if not isinstance(proof, FreshRegistrations):
        raise TypeError('rule_unit_fresh takes a FreshRegistrations, proven now')
    if proof.refused:
        first = proof.refused[0]
        return Ruling('keep', 'registration_unknown' if first.why == 'unknown' else OUTPUT_UNAVAILABLE,
                      f'disposable registration changed ({first.detail})')
    path, proven = unit.evidence[0], set()
    if proof.overlaps or \
            any(repository_tree(Path(name), boundary=path, proven=proven) for name in unit.measured) or \
            not placement.cleanup_contains(run.request.drive['path'], str(unit.target)) or live_tree(unit.target, run):
        return _unavailable('disposable side marker changed')
    return Ruling('candidate', 'declared_disposable')


def rule_temp_entry(run, path) -> Ruling:
    """Stage 2's static checks on one temp entry: a protected root, a live owner, or a repository above it keeps it.
    The scan below it, the scan's cache and its root's budget stay in disk_temp, as does the age."""
    request = run.request
    roots = [Path(request.state_dir).absolute()]
    roots += [Path(placement.native(repo['repo'])).absolute() for repo in request.repos if repo.get('repo')]
    roots += [root.absolute() for repo in request.repos for root in worktree_roots(repo)]
    if not safe_path(path) or live_tree(path, run) or \
            any(within(path, root) or within(root, path) for root in roots) or \
            any(repository_root(parent) for parent in path.parents):
        return Ruling('keep', 'protected', 'protected subtree')
    return Ruling('candidate', 'temp_retained')      # the row's code until its scan, age and process proof are read


def rule_build_unit(run, path, measured, *, entry, processes) -> Ruling:
    """One immediate subfolder of the declared build root `entry`, as its walk `measured` it (files, bytes, age_s, the
    age of its newest path): a link, another drive, or what `rule_temp_entry` protects keeps it, as does activity within
    the entry's `max_idle_hours` or a process `processes` finds using it. `processes` is a proof with the budget
    treatment: a build unit is regenerable output (ledger ruling, external build roots)."""
    request = run.request
    if placement.cleanup_link(path) or not path.is_dir():
        return Ruling('keep', 'protected', 'build unit is a link or not a folder')
    if not placement.cleanup_contains(request.drive['path'], str(path)):
        return Ruling('keep', 'drive_unproven')
    static = rule_temp_entry(run, path)
    if static.action != 'candidate':
        return static
    if measured['age_s'] < entry.get('max_idle_hours', 2) * 3600:
        return Ruling('keep', 'build_root_recent')
    if processes.busy(str(path)):
        return Ruling('keep', process_reason(processes) or 'process_busy')
    return Ruling('candidate', 'build_root_idle')


def rule_build_unit_fresh(run, path, measured, again, *, entry, processes: FreshProcesses) -> Ruling:
    """A build unit's last look before it goes: walked `again` after a process proof taken now, it must be as the first
    pass `measured` it, and pass `rule_build_unit` on that proof."""
    if not isinstance(processes, FreshProcesses):
        raise TypeError('rule_build_unit_fresh takes a FreshProcesses, taken now')
    if again != measured:
        return Ruling('keep', 'revalidation_changed', 'build unit changed since it was measured')
    return rule_build_unit(run, path, again, entry=entry, processes=processes.proof)


def rule_cache_file(run, path, info, *, entry) -> Ruling:
    """One file under the declared cache folder `entry`, its stat `info`: older than the entry's `max_age_days`, or of
    any age when a run that is not daily meets a drive at warn or worse. `facts` hold `activity`, the later of atime and
    mtime, which orders the candidates, and `aggressive`, that level reading."""
    request = run.request
    aggressive = request.mode != 'daily' and request.drive.get('level') in ('warn', 'queue', 'hold', 'critical')
    activity = info.st_atime if info.st_atime > info.st_mtime else info.st_mtime
    facts = dict(activity=activity, aggressive=aggressive)
    if aggressive or request.now - activity > entry.get('max_age_days', 30) * 86400:
        return Ruling('candidate', 'folder_cache', facts=facts)
    return Ruling('keep', 'cache_recent', facts=facts)

