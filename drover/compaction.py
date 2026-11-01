"""The overnight WSL compaction job, run once a night by the elevated task \\DroverCompaction.

Order: lock, hold, stop, kill, trim, shutdown, compact, restart, release, unlock. The job takes the cleanup lock first:
its kill would otherwise reach a cleanup worker mid removal. If a cleanup keeps the lock the night is skipped. Once this
run holds WSL it always restarts WSL and releases the hold, whatever failed. Each step is stored in the hold file first, so a crash leaves a record the hub can
release on its next start. The job cannot open the ledger (ADR-0001) and its workers are the hub's children, so the hub
winds them down when it sees the hold (Dispatcher._compaction_drain) and answers in a file. The result is written to
the state directory, and anything but `compacted` raises an Inbox notice.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

from . import compaction_hold, compaction_vhd, config, disk, disk_state, placement
from .placement_exec import NO_WINDOW

DRAIN_WAIT_S = 7 * 60           # the hub's 5 minute grace, its kill and two dispatcher ticks
DRAIN_POLL_S = 5
AGENT_PROCESS = re.compile(r'\b(claude|codex)\b|drover\.(runner|placement)')
NOTICE_TRIES = 30
LOCK_WAIT_S = 30 * 60           # a running cleanup gets this long to finish; the job's task limit is 3 hours
LOCK_POLL_S = 5


def pane_stop(runtime):
    """Phase 3: stop agents in WSL panes. There are none yet."""


class Runtime:
    """Everything the job does to the machine, all of it through `run`."""

    def __init__(self, cfg, *, run=subprocess.run, clock=time.time, sleep=time.sleep):
        self.cfg, self.run_command, self.clock, self.sleep = cfg, run, clock, sleep
        self.state_dir = config.state_dir(cfg)
        self.distro = disk.tracker_distro(cfg)

    def vhdx(self):
        """This distro's disk, from its registry entry."""
        return placement._wsl_vhdx_paths()[self.distro].removeprefix('\\\\?\\')

    def lock(self):
        """The cleanup lock, waited for up to LOCK_WAIT_S; None when it stays busy."""
        deadline = self.clock() + LOCK_WAIT_S
        while True:
            lock = disk_state.CleanupLock(self.state_dir)
            try:
                lock.__enter__()
                return lock
            except OSError:
                if self.clock() >= deadline:
                    return None
            self.sleep(LOCK_POLL_S)

    def unlock(self, lock):
        lock.__exit__(None, None, None)

    def claim(self, at):
        identity = placement.cleanup_identity()
        return compaction_hold.claim(self.state_dir, at=at, pid=identity.get('pid', os.getpid()),
                                     birth=identity.get('birth'), dead=holder_dead)

    def save(self, record):
        compaction_hold.save(self.state_dir, record)

    def release(self, record):
        compaction_hold.release(self.state_dir, record['id'])

    def wait_for_hub(self, record):
        """The hub's answer to this hold; its last word with drained false when the deadline passes first."""
        deadline = self.clock() + DRAIN_WAIT_S
        while True:
            answer = compaction_hold.read_drain(self.state_dir)
            answer = answer if answer and answer.get('id') == record['id'] else None
            if answer and answer.get('drained'):
                return answer
            if self.clock() >= deadline:
                return answer or dict(stopped=[], killed=[], requeued=[], drained=False)
            self.sleep(DRAIN_POLL_S)

    def _wsl(self, argv, timeout):
        return self.run_command(argv, capture_output=True, text=True, timeout=timeout, **NO_WINDOW)

    def kill(self):
        """Kill every agent or runner process left in WSL, including ones Drover does not know about. Returns PIDs."""
        root = ['-d', self.distro, '-u', 'root', '--']
        done = self._wsl(placement.wsl_control([*root, 'ps', '-eo', 'pid=,args=']), 60)
        if done.returncode:
            raise RuntimeError(f'the WSL process list failed (exit {done.returncode})')
        rows = [line.split(None, 1) for line in str(done.stdout).splitlines() if line.strip()]
        pids = [int(row[0]) for row in rows if len(row) == 2 and row[0].isdigit() and AGENT_PROCESS.search(row[1])]
        if pids:
            self._wsl(placement.wsl_control([*root, 'kill', '-9', *map(str, pids)]), 60)
        return pids

    def trim(self):
        done = self._wsl(placement.trim_argv(), 1800)
        if done.returncode:
            raise RuntimeError(f'fstrim failed (exit {done.returncode})')

    def shutdown(self):
        done = self._wsl(['wsl.exe', '--shutdown'], 120)
        if done.returncode:
            raise RuntimeError(f'wsl --shutdown failed (exit {done.returncode})')

    def compact(self, vhdx):
        answer = compaction_vhd.compact(vhdx, run=self.run_command, sleep=self.sleep,
                                        before_mount=lambda: compaction_hold.note_mount(self.state_dir, vhdx))
        if answer['dismounted']:
            compaction_hold.clear_mount(self.state_dir)
        return answer

    def clear_left_mount(self):
        """A crash can leave the disk attached, and only an elevated process can detach it."""
        vhdx = compaction_hold.mounted(self.state_dir)
        if vhdx:
            failed = compaction_vhd.dismount(vhdx, run=self.run_command)
            if failed:
                raise RuntimeError(f'the disk left mounted by an earlier run would not dismount: {failed}')
            compaction_hold.clear_mount(self.state_dir)

    def restart(self):
        done = self._wsl(['wsl.exe', '-d', self.distro, '--exec', 'true'], 120)
        if done.returncode:
            raise RuntimeError(f'WSL did not restart (exit {done.returncode})')


def holder_dead(record):
    """True only on proof: the process is gone, or its PID now belongs to another process. Unknown is not dead."""
    pid = record.get('pid')
    if type(pid) is not int or pid <= 0:
        return False
    try:
        seen = placement.cleanup_identity(pid).get('birth')
    except (OSError, subprocess.SubprocessError):
        return False
    return seen == 'absent' or bool(seen and record.get('birth') and seen != record['birth'])


def report(state_dir, result, at):
    """The morning report: always a file. An Inbox notice goes through the disk outbox unless the night was clean."""
    disk_state.save_file(state_dir, compaction_hold.REPORT_FILE, {**result, 'at': at})
    if result['status'] == 'compacted' and not result['errors']:
        return
    ident = 'compaction:%s:%s' % (time.strftime('%Y-%m-%d', time.localtime(at)), result['status'])
    why = '; '.join(str(error) for error in result['errors']) or 'no reason recorded'
    outcome = 'compacted with errors' if result['status'] == 'compacted' else result['status']
    notice = dict(id=ident, kind='compaction', title=f'WSL compaction {outcome}',
                  body=f'The overnight WSL compaction {outcome}: {why}')

    def change(state):
        state.setdefault('outbox', {}).setdefault(ident, dict(notice=notice, toasted=False, acknowledged=False))
    for _ in range(NOTICE_TRIES):
        try:
            return disk_state.update(state_dir, change)
        except BlockingIOError:
            time.sleep(1)


def _step(runtime, record, name, phase=None):
    record.update(step=name, at=runtime.clock(), phase=phase or record['phase'], steps=[*record['steps'], name])
    runtime.save(record)


def _note_step(runtime, record, result, name):
    """Record a closing step. A record that cannot be written must not stop what comes next."""
    try:
        _step(runtime, record, name)
    except Exception as error:
        result['errors'].append(f'step {name} was not recorded: {type(error).__name__}: {error}')


def _attempt(result, action):
    """Run a step whose failure must not stop the job."""
    try:
        return action()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        result['errors'].append(str(error))


def _compact(runtime, record, result):
    vhdx = runtime.vhdx()
    _step(runtime, record, 'stop')
    pane_stop(runtime)
    answer = runtime.wait_for_hub(record)
    result['stopped'] = list(answer['stopped'])
    result['requeued'] = list(answer['requeued'])
    result['killed'] = [dict(run=run) for run in answer['killed']]
    if not answer['drained']:
        result['errors'].append('the hub did not confirm WSL work had stopped')
    _step(runtime, record, 'kill')
    result['killed'] += [dict(pid=pid) for pid in _attempt(result, runtime.kill) or []]
    _step(runtime, record, 'trim')
    _attempt(result, runtime.trim)
    _step(runtime, record, 'shutdown', phase='offline')
    runtime.shutdown()
    runtime.clear_left_mount()      # only here: the VHDX cannot be dismounted cleanly under a running WSL
    _step(runtime, record, 'compact')
    return runtime.compact(vhdx)


def _held(runtime, at, result):
    """Hold WSL, compact and let go. Everything the cleanup lock has to cover."""
    try:
        record = runtime.claim(at)
    except Exception as error:          # this run holds nothing, so it has nothing to restart or release
        result.update(status='failed')
        result['errors'].append(f'the hold could not be taken: {type(error).__name__}: {error}')
        return
    if record is None:
        result['errors'].append('another compaction still holds WSL')
        return
    result['status'] = 'failed'
    try:
        answer = _compact(runtime, record, result)
        result.update(status=answer['status'], before_gb=answer['before_gb'], after_gb=answer['after_gb'])
        result['errors'] += answer['errors']
    except Exception as error:          # whatever broke, WSL comes back and the hold goes
        result['status'] = 'failed'
        result['errors'].append(f'{type(error).__name__}: {error}')
    finally:
        _note_step(runtime, record, result, 'restart')
        try:
            runtime.restart()
        except Exception as error:
            result['status'] = 'failed'
            result['errors'].append(f'{type(error).__name__}: {error}')
        _note_step(runtime, record, result, 'release')
        try:
            runtime.release(record)
        except Exception as error:      # the hold then ends by age
            result['status'] = 'failed'
            result['errors'].append(f'the hold was not released: {type(error).__name__}: {error}')


def run(cfg=None, *, runtime=None, now=None) -> dict:
    """One night's job. Returns status ('compacted', 'skipped' or 'failed'), stopped, killed, requeued, before_gb,
    after_gb and errors. The hold starts when the lock is won: the hub's grace counts from it."""
    cfg = config.get() if cfg is None else cfg
    runtime = Runtime(cfg) if runtime is None else runtime
    result = dict(status='skipped', stopped=[], killed=[], requeued=[], before_gb=None, after_gb=None, errors=[])
    lock = runtime.lock()
    at = runtime.clock() if now is None else now
    if lock is None:
        result['errors'].append(f'a disk cleanup kept its lock for {LOCK_WAIT_S // 60} minutes; WSL was left alone tonight')
    else:
        try:
            _held(runtime, at, result)
        finally:
            try:
                runtime.unlock(lock)
            except Exception as error:      # a stale lock is reclaimed once this process is gone
                result['errors'].append(f'the cleanup lock was not released: {type(error).__name__}: {error}')
    report(config.state_dir(cfg), result, at)
    return result


def recover(cfg=None):
    """On hub start, release a hold whose job died and report it. Returns the result, else None."""
    cfg = config.get() if cfg is None else cfg
    state_dir = config.state_dir(cfg)
    record = compaction_hold.read(state_dir)
    if record is None or not holder_dead(record):
        return None
    step = record.get('step', 'unknown')
    result = dict(status='failed', stopped=[], killed=[], requeued=[], before_gb=None, after_gb=None,
                  errors=[f'the job died at step {step}; check that WSL starts'])
    if compaction_hold.mounted(state_dir):
        result['errors'].append('the disk may still be mounted; the next run dismounts it')
    compaction_hold.release(state_dir, record['id'])
    report(state_dir, result, record.get('since', record.get('at', time.time())))
    return result


def main(argv=None):
    argparse.ArgumentParser(prog='drover.compaction', description=__doc__).parse_args(argv)
    result = run()
    json.dump(result, sys.stdout)
    return 1 if result['status'] == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
