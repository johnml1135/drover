"""Install independent user schedules, with report receipts gating applied mode."""
import copy
from pathlib import Path

from . import config, disk_preflight, disk_schedule_steps, disk_state, durable, placement, process
from .placement_exec import locale_text

LONG = 600.0    # git_client.LONG from Task 22; installation had no timeout


def install(cfg: dict | None = None, *, apply=False, dry_run=False) -> dict:
    from .disk_cleanup import scope_key
    cfg = config.get() if cfg is None else cfg
    cfg = copy.deepcopy(cfg)
    path = config.home() / 'config.toml'
    raw = config._read(path)
    seeded = False
    written = cfg['disk_cleanup']
    if raw is not None:
        original = copy.deepcopy(raw)
        if 'disk_cleanup' not in raw:
            raw['disk_cleanup'] = placement.detected_cleanup_policy(config.state_dir(cfg))
        placement.seed_cleanup_process_policy(raw['disk_cleanup'])
        seeded = raw != original
        resolved = config.validate(raw)
        disk_preflight.check_cache_folders(resolved)
        written = raw['disk_cleanup']
        cfg['disk_cleanup'] = resolved['disk_cleanup']      # as run() sees it: the report's scope is hashed over this
    plan = disk_schedule_steps.schedule_plan(cfg, apply=apply)
    plan['disk_cleanup'] = written
    if dry_run:
        return plan
    state_dir = config.state_dir(cfg)
    with disk_state.CleanupLock(state_dir):
        if apply and disk_state.read(state_dir).get('report_scope') != scope_key(cfg):
            raise ValueError('complete a dry report for the same cleanup scope before installing apply')
        if seeded:
            path.parent.mkdir(parents=True, exist_ok=True)
            durable.write_text(path, config.dumps(raw), sync=True)
        for filename, body in plan['files'].items():
            target = Path(filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding='utf-8')
        for argv in plan['argv']:
            done = process.run(process.Command(argv, LONG))
            if done.kind == 'unavailable':     # a missing schtasks or crontab raised its OSError before
                raise OSError(done.detail)
            if not done.ok:                     # a timeout leaves a partial schedule: failed, and no receipt
                raise RuntimeError(f'schedule installation failed: {done.detail or locale_text(done.err, "replace")}')
        disk_state.update(state_dir, lambda state: state.update(automation=dict(apply=bool(apply))))
    return plan
