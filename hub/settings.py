"""Hub settings: paths, ports, auth and watchdog tuning. Paths, ports, the distro, the WSL user, the allowed logins and
the Ollama url default from drover.config (the state dir is %USERPROFILE%\\.drover unless the config says otherwise)."""
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from drover import config


def _cfg(*keys):
    return lambda: config.dig(config.get(), ".".join(keys))


def _state(name):
    return lambda: config.state_path(name)


def _wsl_user():
    # DROVER_WSL_USER / paths.wsl_user, else the Windows user name (override when your WSL user differs)
    return (os.environ.get("DROVER_WSL_USER") or config.get()["paths"]["wsl_user"] or os.environ.get("USERNAME")
            or os.environ.get("USER") or "")


def _ollama_url():
    return config.get()["decider"]["url"] or "http://127.0.0.1:11434"  # native Windows Ollama (plan section 12)


@dataclass
class Settings:
    token_path: Path = field(default_factory=_state("hub-token"))
    ledger_path: Path = field(default_factory=_state("ledger.db"))
    token: str | None = None  # explicit token wins over the file
    ui_host: str = "127.0.0.1"
    ui_port: int = field(default_factory=_cfg("hub", "ui_port"))
    api_port: int = field(default_factory=_cfg("hub", "api_port"))
    wsl_adapter_glob: str = "vEthernet (WSL*)"
    distro: str = field(default_factory=_cfg("paths", "wsl_distro"))
    wsl_user: str = field(default_factory=_wsl_user)
    heartbeat_stale_s: float = 180.0
    watchdog_interval_s: float = 30.0
    keepalive_s: float = 600.0       # length of one WSL keepalive child (`sleep`); renewed while WSL work is due
    wsl_needed_ttl_s: float = 300.0  # how long a dispatcher's 'WSL is needed' note stays live
    wsl_timeout_s: float = 20.0
    wsl_boot_timeout_s: float = 120.0
    trusted_ui_hosts: tuple[str, ...] = ("127.0.0.1", "::1")
    # empty: only the machine owner's Tailscale login, discovered at startup
    ui_allowed_logins: tuple[str, ...] = field(default_factory=lambda: tuple(config.get()["hub"]["allowed_logins"]))
    # hub.tailscale = false: no tailscale lookups, loopback only. auth_mode: "tailscale" (OIDC keys are reserved)
    use_tailscale: bool = field(default_factory=_cfg("hub", "tailscale"))
    auth_mode: str = field(default_factory=_cfg("hub", "auth", "mode"))
    # runs `tailscale <args>` and returns stdout; None uses the real binary
    tailscale: Callable[[list[str]], str] | None = None
    adapter_retry_s: float = 60.0
    boot_cooldown_s: float = 600.0
    ui_allowed_hosts: tuple[str, ...] = ()  # extra Host values (tests, custom names)
    # runs `icacls <argv>` and returns stdout; None uses the real binary on Windows only
    icacls: Callable[[list[str]], str] | None = None
    run_watchdog: bool = True
    # the dispatcher thread: off by default so tests never take the real lock; hub.__main__ turns it on
    run_dispatcher: bool = False
    # the owner-UI services thread (garden executor, tuner check, WSL tool discovery): off for tests
    run_services: bool = False
    ollama_url: str = field(default_factory=_ollama_url)
    dispatcher_lock: Path = field(default_factory=_state("dispatcher.lock"))
    runs_dir: Path = field(default_factory=_state("runs"))
    dispatch_interval_s: float = 30.0
    # ADR 0002: workers reach the hub through files, so the WSL-facing API listener is off unless asked for
    api_listen_wsl: bool = field(default_factory=_cfg("hub", "api_listen_wsl"))
    extra: dict = field(default_factory=dict)
