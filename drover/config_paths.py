"""Configuration home and seat environment paths."""
import os
from pathlib import Path

from .placement import native


def home(env=None):
    """Where config.toml is found: $DROVER_HOME, else ~/.drover."""
    env = os.environ if env is None else env
    return Path(native(env["DROVER_HOME"])) if env.get("DROVER_HOME") else Path.home() / ".drover"


def seat_env(seat):
    """Account selection for a worker or usage probe."""
    if not seat:
        return {}
    path = (seat.get("codex_home") or seat.get("config_dir")) if seat["backend"] == "codex" else seat.get("config_dir")
    path = path or ("~/.claude" if seat["backend"] == "claude" else "~/.codex")
    # Pin a Windows account before a task can move its worker to WSL. WSL seats expand ~ there.
    if seat.get("placement") == "windows":
        path = os.path.expanduser(path)
    return {"CLAUDE_CONFIG_DIR" if seat["backend"] == "claude" else "CODEX_HOME": path}
