"""python -m drover init: the interactive setup wizard. Stdlib input(); detect first, then ask, each default pre-filled.

    python -m drover init [--defaults] [--answers FILE.toml] [--state-dir DIR]

Order: state dir, repos root and worktrees, Ollama model dir, decider models and GPU vs CPU (with a benchmark), seats,
resource tracker levels, WSL distro, hub, repos. It previews the TOML, asks to confirm, backs up any existing file
and writes config.toml and repos/*.toml, only inside the state dir. It never changes anything else: where the
machine needs a change (OLLAMA_MODELS, DROVER_HOME) it prints the command for you to run.
--defaults takes every default; --answers FILE.toml answers questions by key (see ANSWER_KEYS). Every detection is a
callable in the `detect` dict, so tests inject them (real_detect() holds the real ones).
"""
from drover import placement
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from . import config, decisions, repos
from .tool_discovery import DISCOVERY_SCRIPT, parse_discovery

from .wizard_asking import Asker, WizardError, one_of, to_bool, to_float, to_int, to_list, to_str
from .wizard_detect import (BENCH_CALLS, CMD_EXE, _free_gb, _host_ram_wsl, _model_in_vram, _run, _windows_exe,
                            bench_decider, detect_config_dirs, detect_drives, detect_nvidia, detect_ollama,
                            detect_ram_gb, detect_repos, detect_tailscale, detect_tools, detect_worktree_roots,
                            detect_wslconfig_cap, is_windows, real_detect, repo_candidates)
from .wizard_files import (HEADER, _examples_note, _inside, _merge_named, _read_existing, _repo_answer,
                           _resolve_repo, _saved_repo, build_config, example_for, q_repos, repo_toml, write_file)
from .wizard_questions import (REF_RAM_GB, TARGET_P95_MS, _gb, _member, drive_letter, q_decider, q_hub, q_ollama,
                               q_repo_paths, q_seats, q_state_dir, q_trackers, q_wsl, recommend_device,
                               scale_levels, wanted_drives)

ANSWER_KEYS = ("state_dir", "repos_root", "worktrees", "ollama_models", "decider_primary", "decider_shadow",
               "decider_device", "seats", "trackers_accept", "trackers", "wsl_distro", "wsl_user", "ui_port",
               "api_port", "tailscale", "allowed_login", "auth_mode", "repos", "commercial_use", "state_git", "private_remote", "confirm")


def run(detect=None, input_fn=input, out=None, answers=None, defaults=False, state_dir=None, now=time.time):
    """The wizard. Returns 0 when it wrote the files, 1 when the owner declined at the preview."""
    out = out or sys.stdout
    d = {**real_detect(), **(detect or {})}
    a = Asker(input_fn, out, answers, defaults)
    a.say("Drover setup. Detected values are pre-filled; press Enter to accept a default.")
    s = {}
    s["state_dir"] = q_state_dir(a, d, state_dir)
    a.existing = _read_existing(s["state_dir"])
    s["repos_root"], s["worktrees"], repos = q_repo_paths(a, d)
    vhdx = d["vhdx_drives"]()
    s["ollama_models"], ollama = q_ollama(a, d, vhdx)
    s["primary"], s["shadow"], s["device"] = q_decider(a, d, ollama)
    distros = d["distros"]()
    hint = next((x["name"] for x in distros if x["default"] and x["state"] == "Running"), None)
    s["seats"] = q_seats(a, d, hint)
    s["trackers"] = q_trackers(a, d, vhdx, wanted_drives(s, vhdx))
    s["wsl_distro"], s["wsl_user"] = q_wsl(a, d)
    s["ui_port"], s["api_port"], s["tailscale"], s["allowed_login"] = q_hub(a, d)
    progs = q_repos(a, d, repos, s["worktrees"], s["state_dir"])
    a.say("\n10. Decision history: keep config, repo rules and reasons in a private git repo.")
    s["commercial_use"] = a.ask("commercial_use", "Will Drover be used commercially?",
                                 a.prev("decider.commercial_use", False), to_bool)
    a.say("   Some local decision models are licensed non-commercial. Check each model's licence; "
          "for commercial use choose models whose licences permit it.")
    s["state_git"] = a.ask("state_git", "Make the state directory a git repo?", True, to_bool)
    s["private_remote"] = ""
    if s["state_git"]:
        a.say("   A private remote is strongly recommended for backup. It MUST be private; Drover cannot verify visibility.")
        s["private_remote"] = a.ask("private_remote", "Optional private remote URL (empty = local history only)",
                                    a.prev("config_sync.remote", ""))
    try:
        raw = build_config(s, a.existing)
    except config.ConfigError as e:
        raise WizardError(f"the answers do not make a valid config: {e}") from e
    files = {Path(config.native(s["state_dir"])) / "config.toml": HEADER + config.dumps(raw)}
    for p in progs:
        files[Path(config.native(s["state_dir"])) / "repos" / f"{p['name']}.toml"] = repo_toml(p, s["worktrees"])
    a.say("\nPreview")
    for path, text in files.items():
        a.say(f"\n--- {path}\n{text.rstrip()}")
    if not a.ask("confirm", "\nWrite these files?", True, to_bool):
        a.say("Nothing written.")
        return 1
    state = Path(config.native(s["state_dir"]))
    state.mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        backup = write_file(state, path, text, now)
        a.say(f"wrote {path}" + (f" (old file kept as {backup.name})" if backup else ""))
    if s["state_git"]:
        decisions.initialize(state)
    decisions.record(state, "Setup configuration", "Owner reviewed the setup preview and chose these settings.",
                     {"files": [str(p.relative_to(state)) for p in files], "commercial_use": s["commercial_use"]}, "wizard",
                     sync_settings=raw["config_sync"])
    _next_steps(a, s, state)
    return 0


def _next_steps(a, s, state):
    a.say("\nNext:")
    if state != config.home():
        a.say(f"  - set DROVER_HOME so Drover finds this config:  setx DROVER_HOME \"{state}\"   (Install-Drover.ps1 does this)")
    if s["ollama_models"] and s["ollama_models"] != os.environ.get("OLLAMA_MODELS"):
        a.say(f"  - to store models there, run:  setx OLLAMA_MODELS \"{s['ollama_models']}\"   then restart Ollama")
    a.say("  - check it:  python -m drover config --show")


def main(argv=None, detect=None, input_fn=input, out=None):
    ap = argparse.ArgumentParser(prog="python -m drover init")
    ap.add_argument("--defaults", action="store_true", help="accept every default without asking")
    ap.add_argument("--answers", metavar="FILE", help="a TOML file of answers by key (%s)" % ", ".join(ANSWER_KEYS))
    ap.add_argument("--state-dir", help="the state directory to offer as the first default")
    a = ap.parse_args(argv)
    answers = None
    try:
        if a.answers:
            with open(a.answers, "rb") as f:
                answers = tomllib.load(f)
        return run(detect, input_fn, out, answers, a.defaults, a.state_dir)
    except (WizardError, OSError, tomllib.TOMLDecodeError, repos.RepoError, decisions.DecisionError) as e:
        print(f"drover init: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
