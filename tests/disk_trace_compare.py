"""Which way a re-recording moved the decision trace. Only what is provably keep-more passes; the rest is towards_deletion.

A row is provably held only when it says keep or report for a reason in HOLDING. A removal reads as undone only when its
path has stopped going by every witness (`deleted`, a `removed` row, a `completed` record) and something really stopped
being deleted. A new reason, a new action or a removal with less evidence is unknown, and unknown is never yes: it stops
the slice for the lead to rule on.
"""
import json
from collections import Counter
from dataclasses import dataclass

EMPTY = dict(rows=[], errors=[], journal=[], deleted=[])
KEPT = ('keep', 'report')
# Reasons no stage acts on. `orphan` and `unsettled` are kept rows, yet quarantine or an approval can follow them; `settled`,
# `missing`, `quarantine_elapsed` and `lost_registration` are grounds for acting. None of those may join this set.
HOLDING = frozenset((
    'other_side_or_lock_note', 'other_drive', 'main_checkout', 'busy', 'process visibility unavailable', 'protected', 'dirty',
    'live_owner', 'ignored_content', 'nested_worktree', 'identity_changed', 'revalidation_changed', 'unavailable',
    'lost_registration_unproven', 'lost_registration_failed', 'settled_deadline', 'unlocked_worktree', 'prune_expiry_unset',
    'process_out_of_scope', 'process_visibility_unavailable', 'process_query_timeout', 'process_busy', 'temp_root_partial',
    'temp_root_elapsed', 'drive_unproven', 'output_evidence_unavailable', 'repository_unavailable', 'output_deadline',
    'budget_not_needed', 'budget_shortfall', 'budget_deadline', 'budget_used_unknown', 'budget_processes_unknown',
    'worktree_inventory', 'untracked_or_ignored', 'unlisted_folder', 'declared_report', 'registration_unknown'))
# Kept today, yet time, an approval, or a later reading turns them into an act; `budget_usage` is the figure a budget notice is
# judged on, and `merged_live_task` tells the owner work is merged. A reason may enter this set from HOLDING, never leave it for there.
ON_THE_CLOCK = frozenset(('orphan', 'unsettled', 'settled', 'missing', 'quarantine_elapsed', 'lost_registration', 'temp_retained',
                          'budget_usage', 'merged_live_task'))


@dataclass(frozen=True)
class Change:
    scenario: str
    part: str
    old: object
    new: object
    direction: str


def held(row):
    return row['action'] in KEPT and row['reason_code'] in HOLDING


def going(scene):
    """Every path the scene says goes, by any witness: `missing` shows only in its row and journal."""
    return (set(scene['deleted']) | {r.get('path') for r in scene['rows'] if r['action'] == 'removed'}
            | {r['path'] for r in scene['journal'] if r['phase'] == 'completed'})


def spared(before, after):
    """Paths provably no longer going: gone from every witness, and none of the scene's deletions left. A partial shrink spares
    nothing: a registration still deleted with its folder kept, or one removal refused beside another, is for the lead to read."""
    deleted = set(before['deleted'])
    return going(before) - going(after) if deleted and not deleted & set(after['deleted']) else set()


def row_direction(old, new, safe):
    if old is None:
        return 'report_added' if held(new) else 'towards_deletion'
    if old['action'] == 'removed' and old.get('path') not in safe:
        return 'towards_deletion'
    if new is None:
        return 'towards_keep' if old['action'] == 'removed' else 'towards_deletion'     # a row gone may be a protection gone
    if new['action'] not in KEPT:
        return 'towards_deletion'
    if new['reason_code'] == old['reason_code']:
        return 'neutral' if old['action'] in KEPT else 'towards_keep'
    return 'towards_keep' if held(new) else 'towards_deletion'


def paired(before, after):
    """Rows by path and reason, then what is left by path, so a changed reason reads as one changed row."""
    left, pairs, unmatched = list(after), [], []
    for row in before:
        match = next((r for r in left if (r.get('path'), r['reason_code']) == (row.get('path'), row['reason_code'])), None)
        (left.remove(match), pairs.append((row, match))) if match else unmatched.append(row)
    for row in unmatched:
        match = next((r for r in left if r.get('path') == row.get('path')), None)
        if match:
            left.remove(match)
        pairs.append((row, match))
    return pairs + [(None, row) for row in left]


def lost_and_new(before, after):
    old, new = Counter(json.dumps(r, sort_keys=True) for r in before), Counter(json.dumps(r, sort_keys=True) for r in after)
    return [json.loads(r) for r in (old - new).elements()], [json.loads(r) for r in (new - old).elements()]


def compare_scene(name, before, after):
    safe, gone = spared(before, after), going(after)
    changes = [Change(name, 'row', old, new, row_direction(old, new, safe))
               for old, new in paired(before['rows'], after['rows']) if old != new]
    lost, new = lost_and_new(before['errors'], after['errors'])
    changes += [Change(name, 'error', row, None, 'towards_deletion') for row in lost]
    changes += [Change(name, 'error', None, row, 'report_added') for row in new]
    lost, new = lost_and_new(before['journal'], after['journal'])
    changes += [Change(name, 'journal', row, None, 'towards_keep' if row['path'] in safe else 'towards_deletion') for row in lost]
    changes += [Change(name, 'journal', None, row, 'report_added' if row['phase'] in ('planned', 'failed') and row['path'] not in gone
                       else 'towards_deletion') for row in new]
    changes += [Change(name, 'deleted', path, None, 'towards_keep' if path in safe else 'towards_deletion')
                for path in before['deleted'] if path not in after['deleted']]
    changes += [Change(name, 'deleted', None, path, 'towards_deletion') for path in after['deleted'] if path not in before['deleted']]
    return changes


def compare(old, new):
    """Every difference between two recordings with its direction. A scenario gone is one `towards_deletion`, whatever it held."""
    changes = []
    for name in sorted({*old, *new}):
        if name not in new:
            changes.append(Change(name, 'scenario', name, None, 'towards_deletion'))
        else:
            changes += compare_scene(name, old.get(name, EMPTY), new[name])
    return changes
