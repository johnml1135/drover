"""The external build-root scene of stage 2, joined to the other stages' scenes, which `tests.disk_trace` runs."""
from tests.disk_trace_stages import STAGE_SCENES as OTHER_STAGES, on


def build_root(s):
    """One external build root: an idle unit goes; a recent one, a busy one and one holding a link stay."""
    root = s.root / 'cargo-targets'
    s.cfg['disk_cleanup']['build_roots'] = [dict(path=str(root), placement=s.side, max_idle_hours=2)]
    s.file(root / 'idle' / 'debug' / 'app')
    s.file(root / 'recent' / 'debug' / 'app', age=3600)
    s.busy_paths.add(str(s.file(root / 'busy' / 'debug' / 'app').parent))
    s.links.add(s.file(root / 'holds_link' / 'out' / 'app').parent)


STAGE_SCENES = {**OTHER_STAGES, 'build_root': on(2, build_root)}
