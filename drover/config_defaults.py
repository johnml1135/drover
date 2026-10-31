"""Built-in configuration defaults and shared constants."""
import os
import re
import sys

BACKENDS = ("claude", "codex")
PLACEMENTS = ("windows", "wsl")
TRACKER_KINDS = ("ram", "disk", "load", "vram", "wsl")
DEVICES = ("auto", "gpu", "cpu")
AUTH_MODES = ("tailscale",)
SIZES = ("S", "M", "L")

# Process names a disk cleanup never counts as workspace activity. Shells are deliberately excluded.
PROCESS_IGNORE_R11_WINDOWS = ['ctfmon.exe', 'TabTip.exe', 'TextInputHost.exe', 'DefenderSessionHelper.exe',
                              'Wacom_TouchUser.exe', 'Taskmgr.exe', 'SecurityHealthSystray.exe', 'sihost.exe']
PROCESS_IGNORE_WSL = ['systemd', '(sd-pam)']
PROCESS_IGNORE = [*PROCESS_IGNORE_R11_WINDOWS,
                  'csrss.exe', 'winlogon.exe', 'dwm.exe', 'fontdrvhost.exe', 'NVDisplay.Container.exe',
                  'RtkAudUService64.exe', 'WacomHost.exe', 'Wacom_Tablet.exe', 'remoting_desktop.exe',
                  'taskhostw.exe', 'dllhost.exe', *PROCESS_IGNORE_WSL]


def windows_or_wsl(platform, env):
    """The platform fact the default disk trackers depend on: a Windows host, or WSL running on one."""
    return platform == "win32" or bool(env.get("WSL_DISTRO_NAME"))


# Read once here and passed on as an argument.
WINDOWS_OR_WSL = windows_or_wsl(sys.platform, os.environ)

NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
ENV_ALIASES = {"DROVER_OLLAMA_URL": "decider.url", "DROVER_REPOS_DIR": "paths.repos_root",
               "DROVER_WSL_USER": "paths.wsl_user", "DROVER_WSL_DISTRO": "paths.wsl_distro"}

DEFAULT_SEATS = [
    {"name": "claude", "backend": "claude", "priority": 1, "max_workers": 0, "reserve_pct": 10.0,
     "director_reserve_pct": 25.0, "director": True, "placement": "windows", "binary": "claude", "config_dir": ""},
    {"name": "codex", "backend": "codex", "priority": 1, "max_workers": 0, "reserve_pct": 10.0,
     "director_reserve_pct": 0.0, "director": False, "placement": "wsl", "binary": "codex", "config_dir": ""},
]
SEAT_DEFAULTS = {"priority": 1, "max_workers": 0, "reserve_pct": 10.0, "director_reserve_pct": 0.0,
                 "five_hour_base_pct": 5.0, "five_hour_per_hour_pct": 5.0, "director": False, "config_dir": "", "codex_home": "", "sandbox": "workspace-write"}


def host_disk_trackers(windows_or_wsl):
    """Physical disks for a Windows host (including WSL), or the Linux root."""
    # H: holds the WSL VHDX, so free space inside WSL counts for work placed there (floor: H alone).
    disks = [("h", "H:/", (150.0, 100.0, 50.0, 25.0), True), ("c", "C:/", (60.0, 40.0, 25.0, 15.0), False)] \
        if windows_or_wsl else [("root", "/", (150.0, 100.0, 50.0, 25.0), False)]
    return [{"kind": "disk", "name": name, "enabled": True, "path": path,
             **dict(zip(("warn_gb", "queue_gb", "hold_gb", "critical_gb"), levels)),
             "wsl_combined": combined, "host_floor_gb": 10.0}
            for name, path, levels, combined in disks]


DEFAULT_TRACKERS = [
    {"kind": "load", "name": "load", "enabled": True, "queue_per_core": 2.5, "hold_per_core": 3.5},
    {"kind": "ram", "name": "ram", "enabled": True, "queue_base_gb": 6.0, "queue_per_worker_gb": 0.5, "hold_gb": 4.0},
    *host_disk_trackers(WINDOWS_OR_WSL),
    {"kind": "vram", "name": "vram", "enabled": True, "fallback_pct": 80.0, "queue_pct": 0.0, "hold_pct": 0.0},
    {"kind": "wsl", "name": "wsl-up", "enabled": True, "distro": "", "queue_mem_pct": 0.0, "hold_mem_pct": 0.0},
]
DEFAULTS = {
    "disk_cleanup": {"temp_roots": [], "report_roots": [], "caches": [], "worker_timeout_s": 1800,
                     "process_ignore": list(PROCESS_IGNORE), "process_query_timeout_s": 240, "temp_root_budget_s": 300,
                     "cooldown_s": 6 * 3600, "worktree_output_idle_s": 2 * 3600},
    "repos_dir": "",
    "config_sync": {"remote": "", "auto_push": True},
    "paths": {"state_dir": "", "repos_root": "~/repos", "worktrees": "", "wsl_distro": "Ubuntu", "wsl_user": "",
              "wsl_worktrees": "~/work"},
    "seats": DEFAULT_SEATS,
    "seats_policy": {"waste_threshold_pct": 15.0},
    "director": {"seat": "", "model": "", "effort": "high"},
    "models": {
        "ids": {"haiku-5.5": "claude-haiku-5-5", "sonnet-5.5": "claude-sonnet-5-5", "opus-5.5": "claude-opus-5-5"},
        "efforts": {"cheap": "xhigh", "mid": "high", "top": "high", "small_claude": "max", "small_codex": "xhigh"},
        "ladders": {"claude": ["haiku-5.5", "sonnet-5.5", "opus-5.5"], "codex": ["gpt-6-luna", "gpt-6.1-sol"]},
        "start": {"S": 0, "M": 1, "L": 1},
        "review": "sonnet-5.5",
        "director": "opus-5.5",
    },
    "capacity": {"parallel_cap": 4},
    "trackers": DEFAULT_TRACKERS,
    "policy": {"tau": 0.8, "landing_batch_size": 3, "tuner_cadence_days": [1, 1, 2, 3, 5, 7], "push": False},
    "decider": {"commercial_use": False,"url": "", "models": {"primary": "nimble-v3", "shadow": "nimble", "long_state": "nimble"},
                "device": "gpu", "mode": "shadow", "timeout_s": 5.0, "ollama_models": ""},
    "hub": {"ui_port": 8765, "api_port": 8766, "tailscale": True, "allowed_logins": [], "api_listen_wsl": False,
            "auth": {"mode": "tailscale", "issuer": "", "client_id": "", "allowed_emails": []}},
}
RESERVED = {("hub", "auth", "issuer"), ("hub", "auth", "client_id"), ("hub", "auth", "allowed_emails")}


class ConfigError(ValueError):
    pass
