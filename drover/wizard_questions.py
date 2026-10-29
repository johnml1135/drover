"""Interactive setup questions and recommendations."""
import re

from drover import placement
from . import config
from .wizard_asking import WizardError, to_bool, to_float, to_int, one_of
from .wizard_detect import BENCH_CALLS
from .wizard_files import _merge_named

# The thresholds settled in plan section 12 were set for the owner's machine: 64 GB RAM (queue 6 GB free, hold 4 GB),
# Disk levels use fixed physical-drive thresholds. RAM levels scale from the reference RAM.
REF_RAM_GB = 64.0
TARGET_P95_MS = 2000.0   # the decider's latency target (plan section 12)


def _gb(v):
    return "unknown" if v is None else f"{v:.0f} GB"


def q_state_dir(a, d, state_override=None):
    a.say("\n1. State directory: the ledger, run logs, rescue bundles, hub token, config and repos live here.")
    drives = d["drives"]()
    for drive in drives:
        a.say(f"   {drive['path']}  {_gb(drive['free_gb'])} free of {_gb(drive['total_gb'])}")
    default = state_override or str(config.home())
    value = a.ask("state_dir", "State directory", default)
    free = d["free_gb"](value)
    if free is not None and free < 10:
        a.say(f"   WARNING: only {_gb(free)} free where that is.")
    return value


def q_repo_paths(a, d):
    a.say("\n2. Repositories: where your repos live, and where task worktrees go.")
    found = {c: d["repos"](c) for c in d["repo_candidates"]()}
    best = max(found, key=lambda c: len(found[c]), default=config.DEFAULTS["paths"]["repos_root"])
    default = a.prev("paths.repos_root", best if found.get(best) else config.DEFAULTS["paths"]["repos_root"])
    for c, repos in found.items():
        if repos:
            a.say(f"   {c}: {len(repos)} repo(s) with a .git folder")
    root = a.ask("repos_root", "Repos root", default)
    repos = d["repos"](root)
    trees = d["worktree_roots"](root)
    if trees:
        a.say("   existing worktree folders: " + ", ".join(trees))
    a.say("   Worktrees: leave empty for one <repo>.worktrees folder next to each repo, or give one shared root.")
    shared = a.ask("worktrees", "Worktrees root (empty = per repo)", a.prev("paths.worktrees", ""))
    return root, shared, repos


def q_ollama(a, d, vhdx):
    a.say("\n3. Ollama model directory.")
    o = d["ollama"]()
    a.say(f"   OLLAMA_MODELS is {'set to' if o.get('env_set') else 'not set; Ollama uses'} {o['models_dir']}"
          f" ({_gb(o.get('free_gb'))} free)")
    value = a.ask("ollama_models", "Ollama model directory", a.prev("decider.ollama_models", o["models_dir"]))
    drive = re.match(r"^([A-Za-z]):", value)
    for distro, vd in vhdx.items():
        if drive and drive.group(1).upper() == vd[0]:
            a.say(f"   WARNING: {value} is on {vd}, the drive holding the WSL disk of {distro}. Model downloads and the "
                  "VHDX compete for that space; prefer another drive.")
    return value, o


def recommend_device(gpu, gpu_bench, cpu_bench):
    """('gpu'|'cpu'|'auto', why). No GPU or no GPU result: cpu. Both benchmarked: cpu when it meets the latency target
    and is not much slower (it leaves the GPU free); otherwise auto (gpu, falling back to cpu when VRAM is over 80%)."""
    if cpu_bench and cpu_bench.get("honoured") is False:
        cpu_bench = None
        note = "the CPU model variant stayed in VRAM, so the CPU timing is not a CPU timing; "
    else:
        note = ""
    if not gpu:
        return "cpu", "no NVIDIA GPU found"
    if cpu_bench is None and note:
        return "gpu", note + "check the CPU variants before selecting cpu or auto"
    if not gpu_bench and not cpu_bench:
        return "auto", "no benchmark (Ollama not reachable); auto uses the GPU and falls back to CPU when VRAM is over 80%"
    if gpu_bench and cpu_bench and cpu_bench["p95_ms"] <= TARGET_P95_MS and cpu_bench["p95_ms"] <= 1.5 * gpu_bench["p95_ms"]:
        return "cpu", f"CPU p95 {cpu_bench['p95_ms']:.0f} ms meets the {TARGET_P95_MS:.0f} ms target and keeps the GPU free"
    return "auto", "the GPU is clearly faster; auto falls back to CPU when VRAM is over 80%"


def q_decider(a, d, ollama):
    a.say("\n4. Decider (the local model for small judgement calls).")
    primary = a.ask("decider_primary", "Primary model",
                    a.prev("decider.models.primary", config.DEFAULTS["decider"]["models"]["primary"]))
    shadow = a.ask("decider_shadow", "Shadow comparison model",
                   a.prev("decider.models.shadow", config.DEFAULTS["decider"]["models"]["shadow"]))
    gpu = d["nvidia"]()
    if gpu:
        a.say(f"   GPU: {gpu['name']}, {gpu['total_mb'] / 1024:.0f} GB VRAM, {100 * gpu['used_mb'] / gpu['total_mb']:.0f}% in use")
    else:
        a.say("   GPU: none found (nvidia-smi)")
    models = {"primary": primary, "shadow": shadow,
              "long_state": a.prev("decider.models.long_state", config.DEFAULTS["decider"]["models"]["long_state"])}
    url = a.prev("decider.url", "") or ollama.get("url")
    ensured = attempted = False

    def ensure():
        nonlocal ensured, attempted
        if attempted:
            return ensured
        attempted = True
        if ollama.get("up"):
            try:
                d["ensure_cpu_models"](models.values(), base=url)
                ensured = True
                a.say("   CPU model variants are ready (-cpu, num_gpu=0).")
            except Exception as e:
                a.say(f"   Could not prepare CPU model variants: {e}")
        if not ensured:
            a.say("   Run python -m drover.decide ensure-cpu-models when Ollama is available, before CPU use.")
        return ensured

    gpu_b = cpu_b = None
    if ollama.get("up") and "decider_device" not in a.answers:
        for device in (["gpu"] if gpu else []) + ["cpu"]:
            if device == "cpu" and not ensure():
                continue
            a.say(f"   benchmarking {BENCH_CALLS} calls on {device} ...")
            res = d["bench"](device, url, models=models)
            if device == "gpu":
                gpu_b = res
            else:
                cpu_b = res
            a.say(f"     {device}: " + (f"p50 {res['p50_ms']:.0f} ms, p95 {res['p95_ms']:.0f} ms" if res else "no answer"))
            if res and res.get("honoured") is False:
                a.say("     NOTE: the CPU model variant is still in VRAM; check its num_gpu=0 parameter.")
    elif not ollama.get("up"):
        a.say("   Ollama is not reachable, so no benchmark was run.")
    rec, why = recommend_device(gpu, gpu_b, cpu_b)
    a.say(f"   recommendation: {rec} ({why})")
    device = a.ask("decider_device", "Decider device (auto|gpu|cpu)", a.prev("decider.device", rec),
                   one_of(*config.DEVICES))
    if device in ("cpu", "auto"):
        ensure()
    if device == "cpu":
        a.say("   On CPU, tev1 is recommended for high-volume yes/no checks (set decider.models.shadow or primary to it).")
    return primary, shadow, device


def q_seats(a, d, distro_hint):
    a.say("\n5. Seats: each is a Claude or Codex account the dispatcher can send workers to.")
    tools = d["tools"](distro_hint)
    dirs = d["config_dirs"]()
    probed = tools.get("_probed", {"windows": True, "wsl": True})
    for t in config.BACKENDS:
        where = tools[t]
        a.say(f"   {t}: Windows {where['windows'] or ('not found' if probed['windows'] else 'not checked')}; "
              f"WSL {where['wsl'] or ('not found' if probed['wsl'] else 'not checked')}")
    for k, v in dirs.items():
        a.say(f"   {k} is set to {v}")
    if "seats" in a.answers:
        raw = a.answers["seats"]
        a.say(f"   seats: {len(raw)} from the answers file")
        return config._seats(_merge_named(a.existing.get("seats", []), raw))
    base = config._seats(a.existing["seats"]) if "seats" in a.existing else config.DEFAULT_SEATS
    seats, prompted = [], set()
    for seat in base:
        t = seat["backend"]
        if t in prompted:
            seats.append(dict(seat))
            continue
        prompted.add(t)
        if not a.use_defaults and not any(tools[t].values()) and all(probed.values()):
            if not a.ask(f"seat_{t}", f"No {t} found. Add a {t} seat anyway?", True, to_bool):
                continue
        n = a.ask(f"seat_{t}_name", f"{t} seat name", seat["name"])
        priority = a.ask(f"seat_{t}_priority", f"{n}: priority (lower is tried first; equal = balance by headroom)",
                         seat["priority"], to_int(0, 999))
        workers = a.ask(f"seat_{t}_max_workers", f"{n}: max workers (0 = no per-seat limit)", seat["max_workers"],
                        to_int(0, 64))
        reserve = a.ask(f"seat_{t}_reserve_pct", f"{n}: allowance reserve %", seat["reserve_pct"], to_float(0, 100))
        dir_key = "codex_home" if t == "codex" and seat.get("codex_home") else "config_dir"
        env_dir = seat.get(dir_key) or dirs.get("CLAUDE_CONFIG_DIR" if t == "claude" else "CODEX_HOME", "")
        cdir = a.ask(f"seat_{t}_config_dir", f"{n}: config dir (CLAUDE_CONFIG_DIR / CODEX_HOME; empty = default)", env_dir)
        placement = seat["placement"]
        if t == "claude" and "seats" not in a.existing:
            placement = "wsl" if not tools[t]["windows"] and tools[t]["wsl"] and probed["windows"] else "windows"
            if placement == "wsl":
                a.say("   claude is only in WSL; its seat will run there.")
        seats.append({**seat, "name": n, "priority": priority, "max_workers": workers, "reserve_pct": reserve,
                      "placement": placement, dir_key: cdir})
    claude = [s["name"] for s in seats if s["backend"] == "claude"]
    if not claude:
        raise WizardError("the director must be a Claude seat; add a claude seat")
    prior = next((s["name"] for s in base if s["director"] and s["name"] in claude), claude[0])
    director = a.ask("director", "Director seat (Claude only)", prior, lambda v: _member(v, claude))
    for s in seats:
        was_director = s["director"]
        s["director"] = s["name"] == director
        if s["director"] != was_director:
            s["director_reserve_pct"] = 25.0 if s["director"] else 0.0
    return config._seats(seats)


def _member(v, options):
    v = str(v).strip()
    if v not in options:
        raise ValueError("one of " + ", ".join(options))
    return v


def scale_levels(ram_gb, drives, wsl_cap, vhdx_letters, wanted=None):
    """Scale RAM levels; use fixed physical-disk thresholds regardless of capacity."""
    ts, notes = [], []
    if ram_gb:
        k = ram_gb / REF_RAM_GB
        ts.append({"kind": "ram", "name": "ram", "queue_base_gb": max(2.0, round(6.0 * k, 1)),
                   "hold_gb": max(1.5, round(4.0 * k, 1)), "queue_per_worker_gb": 0.5})
        notes.append(f"RAM {ram_gb:.0f} GB: queue below {ts[-1]['queue_base_gb']:g} GB free, hold below {ts[-1]['hold_gb']:g} GB")
    for d in drives:
        letter = d["path"][0].upper()
        if wanted is not None and letter not in wanted:
            continue
        levels = (60.0, 40.0, 25.0, 15.0) if letter == "C" else (150.0, 100.0, 50.0, 25.0)
        vhdx = letter in vhdx_letters
        ts.append({"kind": "disk", "name": letter.lower(), "path": d["path"],
                   **dict(zip(config.DISK_LEVEL_KEYS, levels))})
        notes.append(f"{d['path']} {d['total_gb']:.0f} GB: queue below {ts[-1]['queue_gb']:g} GB free, hold below "
                     f"{ts[-1]['hold_gb']:g} GB" + (" (hosts the WSL disk)" if vhdx else ""))
        if d["free_gb"] is not None and d["free_gb"] < ts[-1]["warn_gb"]:
            notes.append(f"WARNING: {d['path']} has only {d['free_gb']:.0f} GB free, below its warning level; free space there")
    for default in placement.default_disk_trackers():
        if default["name"] not in {t["name"] for t in ts}:
            ts.append({**default, "enabled": False})
    ts.append({"kind": "wsl", "name": "wsl-up", "queue_mem_pct": 85.0 if wsl_cap else 0.0,
               "hold_mem_pct": 95.0 if wsl_cap else 0.0})
    if wsl_cap:
        notes.append(f".wslconfig caps WSL at {wsl_cap:g} GB: WSL work queues above 85% of it, holds above 95%")
    return ts, notes


def drive_letter(path):
    m = re.match(r"^([A-Za-z]):", str(path or ""))
    return m.group(1).upper() if m else None


def wanted_drives(s, vhdx):
    """The drives Drover actually uses: the state dir's, the repos root's, the worktrees root's, the WSL disk's and C:."""
    letters = {drive_letter(s["state_dir"]), drive_letter(s["repos_root"]), drive_letter(s["worktrees"]), "C"}
    letters |= {v[0] for v in vhdx.values()}
    return {x for x in letters if x}


def q_trackers(a, d, vhdx, wanted):
    a.say("\n6. Resource tracker levels (queue = no new workers, hold = no new workers or CI).")
    cores = d["cores"]()
    a.say(f"   {cores} cores; load stays at 2.5 (queue) and 3.5 (hold) per core")
    a.say("   tracked drives: " + ", ".join(sorted(x + ":" for x in wanted)) + " (state dir, repos, worktrees, WSL disk, C:)")
    trackers, notes = scale_levels(d["ram_gb"](), d["drives"](), d["wslconfig_cap_gb"](), {v[0] for v in vhdx.values()},
                                   wanted)
    for n in notes:
        a.say("   " + n)
    if "trackers" in a.answers:
        return config._trackers(_merge_named(a.existing.get("trackers", []), a.answers["trackers"]))
    if "trackers" in a.existing:
        trackers = config._trackers(a.existing["trackers"])
        a.say("   Keeping existing tracker levels as the defaults:\n" + config.dumps({"trackers": trackers}))
    if a.ask("trackers_accept", "Use these levels?", True, to_bool):
        return config._trackers(trackers)
    for t in trackers:
        if t["kind"] == "ram":
            t["queue_base_gb"] = a.ask("ram_queue", "RAM: queue below (GB free)", t["queue_base_gb"], to_float(0))
            t["hold_gb"] = a.ask("ram_hold", "RAM: hold below (GB free)", t["hold_gb"], to_float(0))
        elif t["kind"] == "disk" and t.get("enabled", True) and "queue_gb" in t:
            t["queue_gb"] = a.ask(f"disk_{t['name']}_queue", f"{t['path']} queue below (GB free)", t["queue_gb"], to_float(0))
            t["hold_gb"] = a.ask(f"disk_{t['name']}_hold", f"{t['path']} hold below (GB free)", t["hold_gb"], to_float(0))
    return config._trackers(trackers)


def q_wsl(a, d):
    a.say("\n7. WSL distro.")
    distros = d["distros"]()
    for x in distros:
        a.say(f"   {'*' if x['default'] else ' '} {x['name']}  {x['state']}  WSL{x['version']}")
    default = a.prev("paths.wsl_distro", next((x["name"] for x in distros if x["default"]), distros[0]["name"]
                                             if distros else config.DEFAULTS["paths"]["wsl_distro"]))
    distro = a.ask("wsl_distro", "WSL distro for the worker pool", default)
    user = a.ask("wsl_user", "WSL user (empty = same as your Windows user)", a.prev("paths.wsl_user", ""))
    return distro, user


def q_hub(a, d):
    a.say("\n8. Hub (the owner website and API).")
    ui = a.ask("ui_port", "UI port", a.prev("hub.ui_port", config.DEFAULTS["hub"]["ui_port"]), to_int(1, 65535))
    api = a.ask("api_port", "WSL API port (off unless api_listen_wsl is on)",
                a.prev("hub.api_port", config.DEFAULTS["hub"]["api_port"]), to_int(1, 65535))
    ts = d["tailscale"]()
    a.say(f"   Tailscale: {'running' if ts['up'] else 'not running'}" + (f", login {ts['login']}" if ts.get("login") else ""))
    use = a.ask("tailscale", "Reach the hub over Tailscale?", a.prev("hub.tailscale", bool(ts["up"])), to_bool)
    logins = a.prev("hub.allowed_logins", [""])
    login = a.ask("allowed_login", "Allowed Tailscale login (empty = this machine's owner)", logins[0] if logins else "") \
        if use else ""
    a.say("   Auth mode: tailscale (oidc: coming later; the issuer, client_id and allowed_emails keys are reserved)")
    a.ask("auth_mode", "Auth mode", "tailscale", one_of("tailscale"))
    return ui, api, use, login
