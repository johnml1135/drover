"""Install independent user schedules, with report receipts gating applied mode."""
import copy
import os
from pathlib import Path
import subprocess

from . import config, disk_state, placement


def install(cfg: dict | None = None, *, apply=False, dry_run=False, runner=None) -> dict:
    from .disk_cleanup import scope_key
    cfg = config.get() if cfg is None else cfg
    cfg = copy.deepcopy(cfg)
    path = config.home() / 'config.toml'
    raw = config._read(path)
    seeded = False
    if raw is not None:
        original = copy.deepcopy(raw)
        if 'disk_cleanup' not in raw:
            raw['disk_cleanup'] = placement.detected_cleanup_policy(config.state_dir(cfg))
        placement.seed_cleanup_process_policy(raw['disk_cleanup'])
        seeded = raw != original
        config.validate(raw)
        cfg['disk_cleanup'] = raw['disk_cleanup']
    plan = placement.disk_schedule_plan(cfg, apply=apply)
    plan['disk_cleanup'] = cfg['disk_cleanup']
    if dry_run:
        return plan
    state_dir = config.state_dir(cfg)
    with disk_state.CleanupLock(state_dir):
        if apply and disk_state.read(state_dir).get('report_scope') != scope_key(cfg):
            raise ValueError('complete a dry report for the same cleanup scope before installing apply')
        if seeded:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.toml.tmp')
            with temporary.open('w', encoding='utf-8') as stream:
                stream.write(config.dumps(raw))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        for filename, body in plan['files'].items():
            target = Path(filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding='utf-8')
        for argv in plan['argv']:
            done = (runner or subprocess.run)(argv, capture_output=True, text=True, shell=False)
            if done.returncode:
                raise RuntimeError(f'schedule installation failed: {done.stderr}')
        disk_state.update(state_dir, lambda state: state.update(automation=dict(apply=bool(apply))))
    return plan
