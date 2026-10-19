"""python -m drover init: the interactive setup wizard. Stdlib input(); detect first, then ask, each default pre-filled.

    python -m drover init [--defaults] [--answers FILE.toml] [--state-dir DIR]

Order: state dir, repos root and worktrees, Ollama model dir, decider models and GPU vs CPU (with a benchmark), seats,
resource tracker levels, WSL distro, hub, repos. It previews the TOML, asks to confirm, backs up any existing file
and writes config.toml and repos/*.toml, only inside the state dir. It never changes anything else: where the
machine needs a change (OLLAMA_MODELS, DROVER_HOME) it prints the command for you to run.
--defaults takes every default; --answers FILE.toml answers questions by key (see ANSWER_KEYS). Every detection is a
callable in the `detect` dict, so tests inject them (real_detect() holds the real ones).
"""
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
from .winproc import NO_WINDOW

# The thresholds settled in plan section 12 were set for the owner's machine: 64 GB RAM (queue 6 GB free, hold 4 GB),
# C: of 476 GB (queue 50, hold 25) and a 931 GB data drive (queue 75, hold 25). Levels scale from those, so that machine
# gets exactly today's numbers and others get the same ratios.
REF_RAM_GB = 64.0
REF_DISKS = {"system": (476.0, 50.0, 25.0), "data": (931.0, 75.0, 25.0)}   # total GB, queue GB, hold GB
TARGET_P95_MS = 2000.0   # the decider's latency target (plan section 12)
BENCH_CALLS = 10
ANSWER_KEYS = ("state_dir", "repos_root", "worktrees", "ollama_models", "decider_primary", "decider_shadow",
               "decider_device", "seats", "trackers_accept", "trackers", "wsl_distro", "wsl_user", "ui_port",
               "api_port", "tailscale", "allowed_login", "auth_mode", "repos", "commercial_use", "state_git", "private_remote", "confirm")
GATE_RE = re.compile(r"`([^`\n]*(?:unittest|pytest|dotnet test|npm (?:run )?test|test\.ps1|pg\.ps1|cargo test|go test)"
                     r"[^`\n]*)`")


class WizardError(Exception):
    pass


# ---- real detections (each returns None or an empty value when unknown; none changes anything) -------------------

def _run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None


def _decode(raw):
    return raw.decode("utf-16-le", "ignore").replace("\x00", "") if b"\x00" in raw[:4] else raw.decode("utf-8", "ignore")


def is_windows():
    return os.name == "nt"


def detect_drives():
    """[{'path': 'C:\\\\', 'free_gb', 'total_gb'}] for each fixed drive (Windows) or mounted /mnt/<x> (WSL)."""
    out = []
    if is_windows():
        import ctypes
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{letter}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:
                u = shutil.disk_usage(root)
                out.append({"path": root, "free_gb": u.free / 1024 ** 3, "total_gb": u.total / 1024 ** 3})
    else:
        for p in sorted(Path("/mnt").glob("[a-z]")):
            try:
                u = shutil.disk_usage(p)
            except OSError:
                continue
            out.append({"path": f"{p.name.upper()}:\\", "free_gb": u.free / 1024 ** 3, "total_gb": u.total / 1024 ** 3})
    return out


def _free_gb(path):
    p = Path(config.native(path))
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 1024 ** 3
    except OSError:
        return None


def detect_repos(root):
    """[{'name', 'path'}] of the directories directly under root that hold a .git."""
    base = Path(config.native(root))
    try:
        return [{"name": d.name, "path": str(root).rstrip("\\/") + ("\\" if "\\" in str(root) else "/") + d.name}
                for d in sorted(base.iterdir()) if (d / ".git").exists() and not d.name.endswith(".worktrees")]
    except OSError:
        return []


def detect_worktree_roots(root):
    base = Path(config.native(root))
    try:
        return [d.name for d in sorted(base.iterdir()) if d.is_dir() and d.name.endswith(".worktrees")]
    except OSError:
        return []


def repo_candidates():
    home = Path.home()
    cands = [config.DEFAULTS["paths"]["repos_root"], str(home / "source" / "repos"), str(home / "repos"),
             str(home / "src"), str(Path.cwd().parent)]
    return list(dict.fromkeys(cands))


def detect_vhdx_drives():
    """{distro: 'R:'} for the drive holding each distro's ext4.vhdx, from the Lxss registry key."""
    out = {}
    if is_windows():
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss")
            for i in range(winreg.QueryInfoKey(key)[0]):
                with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                    try:
                        name = winreg.QueryValueEx(sub, "DistributionName")[0]
                        base = winreg.QueryValueEx(sub, "BasePath")[0]
                    except OSError:
                        continue
                    m = re.match(r"^(?:\\\\\?\\)?([A-Za-z]):", base)
                    if m:
                        out[name] = m.group(1).upper() + ":"
        except OSError:
            pass
        return out
    from . import resources
    host = resources.vhdx_dir()
    m = re.match(r"^/mnt/([a-z])", host or "")
    if m and os.environ.get("WSL_DISTRO_NAME"):
        out[os.environ["WSL_DISTRO_NAME"]] = m.group(1).upper() + ":"
    return out


def detect_nvidia():
    """{'name', 'used_mb', 'total_mb'} from nvidia-smi, or None."""
    for exe in (["nvidia-smi.exe"] if is_windows() else ["nvidia-smi", "nvidia-smi.exe"]):
        r = _run([exe, "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"])
        if r and r.returncode == 0 and r.stdout.strip():
            try:
                name, used, total = (x.strip() for x in _decode(r.stdout).splitlines()[0].split(","))
                return {"name": name, "used_mb": float(used), "total_mb": float(total)}
            except ValueError:
                continue
    return None


def detect_ollama():
    """{'url', 'up', 'models_dir', 'free_gb'}: where Ollama is, whether it answers, and its model store."""
    from . import decide
    url = decide.base_url()
    up = False
    try:
        import urllib.request
        with urllib.request.urlopen(url + "/api/tags", timeout=2) as r:
            up = r.status == 200
    except (OSError, ValueError):
        pass
    models = os.environ.get("OLLAMA_MODELS") or str(Path.home() / ".ollama" / "models")
    return {"url": url, "up": up, "models_dir": models, "env_set": bool(os.environ.get("OLLAMA_MODELS")),
            "free_gb": _free_gb(models)}


def _model_in_vram(url):
    """GPU residency of the CPU decision models, or None when they are not loaded/known."""
    import urllib.request
    from . import decide
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/ps", timeout=3) as r:
            models = json.load(r).get("models", [])
    except (OSError, ValueError):
        return None
    expected = {decide._tagged(decide.cpu_model(m)) for m in config.get()["decider"]["models"].values()}
    models = [m for m in models if decide._tagged(m.get("name") or m.get("model", "")) in expected]
    return any((m.get("size_vram") or 0) > 0 for m in models) if models else None


def bench_decider(device, url=None, models=None):
    """Benchmark decisions after an untimed warm-up; check CPU variants' GPU residency."""
    from . import decide
    previous = config.get()
    if models:
        config.use({**previous, "decider": {**previous["decider"], "models": models}})
    try:
        decide.bench(1, base=url, device=device)
        out = decide.bench(BENCH_CALLS, base=url, device=device)
        if not out["ok"]:
            return None
        if device == "cpu":
            in_vram = _model_in_vram(url or decide.base_url())
            out["honoured"] = None if in_vram is None else not in_vram
        return out
    finally:
        config.use(previous)


CMD_EXE = "/mnt/c/Windows/System32/cmd.exe"


def _windows_exe(tool):
    """The native Windows executable for a tool, or None. Only .exe/.com count: the launcher refuses .cmd shims. From
    WSL this asks Windows through interop (`cmd.exe /c where`); returns (path|None, probed)."""
    if is_windows():
        exe = shutil.which(tool + ".exe") or shutil.which(tool)
        return (exe if exe and exe.lower().endswith((".exe", ".com")) else None), True
    if not Path(CMD_EXE).exists():
        return None, False
    r = _run([CMD_EXE, "/c", "where", tool + ".exe"], 20)
    if r is None:
        return None, False
    lines = [x.strip() for x in _decode(r.stdout).splitlines() if x.strip().lower().endswith((".exe", ".com"))]
    return (lines[0] if r.returncode == 0 and lines else None), True


def detect_tools(distro=None):
    """{'claude': {'windows': path|None, 'wsl': path|None}, 'codex': ..., '_probed': {'windows': bool, 'wsl': bool}}.
    `_probed` says whether each side could be asked: an unprobed side is not "not found". On Windows, WSL is asked only
    when the distro is already running (asking boots it)."""
    out = {t: {"windows": None, "wsl": None} for t in config.BACKENDS}
    probed = {"windows": False, "wsl": False}
    on_windows = is_windows()
    for t in out:
        out[t]["windows"], probed["windows"] = _windows_exe(t)
    if not on_windows:
        probed["wsl"] = True
        r = _run(["bash", "-lc", DISCOVERY_SCRIPT], 30)
        found = parse_discovery(_decode(r.stdout)) if r and r.returncode == 0 else {}
        for t in out:
            out[t]["wsl"] = found.get(t)
    elif distro and any(d["name"] == distro and d["state"] == "Running" for d in detect_distros()):
        probed["wsl"] = True
        r = _run(["wsl.exe", "-d", distro, "--exec", "bash", "-lc", DISCOVERY_SCRIPT], 30)
        found = parse_discovery(_decode(r.stdout)) if r and r.returncode == 0 else {}
        for t in out:
            out[t]["wsl"] = found.get(t)
    out["_probed"] = probed
    return out


def detect_config_dirs():
    return {k: os.environ[k] for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME") if os.environ.get(k)}


def _host_ram_wsl():
    """The Windows host's RAM from inside WSL (MemTotal there is only the VM's share), or None."""
    r = _run(["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile", "-Command",
              "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"], 30)
    try:
        return int(_decode(r.stdout).strip()) / 1024 ** 3 if r and r.returncode == 0 else None
    except ValueError:
        return None


def detect_ram_gb():
    if not is_windows() and Path("/mnt/c/Windows/System32").is_dir():
        host = _host_ram_wsl()
        if host:
            return host
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 ** 2
    except (OSError, ValueError):
        pass
    if is_windows():
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong),
                        ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ave", ctypes.c_ulonglong)]
        st = Status()
        st.length = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.total / 1024 ** 3
    return None


def detect_wslconfig_cap():
    from . import resources
    path = resources.wslconfig_path()
    try:
        return resources.parse_wslconfig_memory_gb(path.read_text(errors="replace")) if path else None
    except OSError:
        return None


def parse_distros(text):
    """[{'name', 'state', 'version', 'default'}] from `wsl -l -v` output."""
    out = []
    for line in text.replace("\x00", "").splitlines()[1:]:
        m = re.match(r"^\s*(\*)?\s*(\S.*?)\s{2,}(\w+)\s+(\d)\s*$", line)
        if m:
            out.append({"name": m.group(2), "state": m.group(3), "version": int(m.group(4)), "default": bool(m.group(1))})
    return out


def detect_distros():
    if not is_windows():
        name = os.environ.get("WSL_DISTRO_NAME")
        return [{"name": name, "state": "Running", "version": 2, "default": True}] if name else []
    r = _run(["wsl.exe", "-l", "-v"], 20)
    return parse_distros(_decode(r.stdout)) if r and r.returncode == 0 else []


def detect_tailscale():
    """{'up': bool, 'login': str|None}."""
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        r = _run([exe, "status", "--json"], 20)
        if r and r.returncode == 0:
            try:
                d = json.loads(_decode(r.stdout))
                return {"up": d.get("BackendState") == "Running",
                        "login": d["User"][str(d["Self"]["UserID"])]["LoginName"]}
            except (ValueError, KeyError, TypeError):
                return {"up": False, "login": None}
    return {"up": False, "login": None}


def detect_gate_hint(repo):
    """(command line, where it came from) suggested for a repo, or None: AGENTS.md first, then test.ps1, pg.ps1."""
    base = Path(config.native(repo))
    try:
        m = GATE_RE.search((base / "AGENTS.md").read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1).strip(), "AGENTS.md"
    except OSError:
        pass
    for script in ("test.ps1", "pg.ps1"):
        if (base / script).is_file():
            return script, script
    return None


def detect_repo_kind(repo):
    """'windows' when the repo holds .NET or PowerShell build files, else None (no opinion)."""
    base = Path(config.native(repo))
    try:
        names = {p.name.lower() for p in base.iterdir()}
    except OSError:
        return None
    return "windows" if any(n.endswith((".sln", ".csproj", ".ps1")) for n in names) else None


def real_detect():
    from . import decide
    return {"windows": is_windows, "home": Path.home, "drives": detect_drives, "free_gb": _free_gb,
            "repo_candidates": repo_candidates, "repos": detect_repos, "worktree_roots": detect_worktree_roots,
            "vhdx_drives": detect_vhdx_drives, "nvidia": detect_nvidia, "ollama": detect_ollama,
            "bench": bench_decider, "ensure_cpu_models": decide.ensure_cpu_models, "tools": detect_tools,
            "config_dirs": detect_config_dirs,
            "ram_gb": detect_ram_gb, "cores": lambda: os.cpu_count() or 1, "wslconfig_cap_gb": detect_wslconfig_cap,
            "distros": detect_distros, "tailscale": detect_tailscale, "gate_hint": detect_gate_hint,
            "repo_kind": detect_repo_kind, "exists": lambda p: Path(config.native(p)).exists()}


# ---- asking ---------------------------------------------------------------------------------------------------

def to_bool(v):
    if isinstance(v, bool):
        return v
    low = str(v).strip().lower()
    if low in ("y", "yes", "true", "1", "on"):
        return True
    if low in ("n", "no", "false", "0", "off"):
        return False
    raise ValueError("answer yes or no")


def to_int(lo=None, hi=None):
    def conv(v):
        try:
            n = int(str(v).strip()) if not isinstance(v, int) or isinstance(v, bool) else v
        except ValueError:
            raise ValueError("a whole number, please") from None
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            raise ValueError(f"between {lo} and {hi}, please")
        return n
    return conv


def to_float(lo=None, hi=None):
    def conv(v):
        try:
            n = float(v)
        except (TypeError, ValueError):
            raise ValueError("a number, please") from None
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            raise ValueError(f"between {lo} and {hi}, please")
        return n
    return conv


def one_of(*options):
    def conv(v):
        v = str(v).strip().lower()
        if v not in options:
            raise ValueError("one of " + ", ".join(options))
        return v
    return conv


def to_str(v):
    return str(v).strip()


def to_list(v):
    return [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else [str(x) for x in v]


class Asker:
    def __init__(self, input_fn, out, answers=None, defaults=False):
        self.input, self.out, self.answers, self.use_defaults = input_fn, out, answers or {}, defaults
        self.existing = {}     # the explicit values of a config.toml already in the state dir: they pre-fill the answers

    def prev(self, dotted, default):
        """The existing config's value for a dotted key, else the detected default."""
        value = self.existing
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def say(self, text=""):
        print(text, file=self.out)

    def ask(self, key, prompt, default, conv=to_str):
        """The answer for `key`: from the answers file, else the default (--defaults), else the user (empty line = default)."""
        shown = (" [Y/n]" if default else " [y/N]") if isinstance(default, bool) else \
            "" if default in (None, "") else f" [{default}]"
        if key in self.answers:
            try:
                value = conv(self.answers[key])
            except ValueError as e:
                raise WizardError(f"answers file, {key}: {e}") from e
            self.say(f"{prompt}{shown}: {value}  (from answers)")
            return value
        if self.use_defaults:
            value = conv(default) if default not in (None, "") else default
            self.say(f"{prompt}{shown}: {'yes' if value is True else 'no' if value is False else value}  (default)")
            return value
        while True:
            try:
                raw = self.input(f"{prompt}{shown}: ").strip()
            except EOFError:
                raise WizardError("input ended before the questions were answered (use --defaults or --answers)") \
                    from None
            if raw == "" and default not in (None,):
                return conv(default) if default != "" else ""
            try:
                return conv(raw)
            except ValueError as e:
                self.say(f"  {e}")


# ---- the questions --------------------------------------------------------------------------------------------

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
    """Tracker overrides scaled to the machine from the REF_* machine: RAM levels with total RAM, disk levels with drive
    size (C: from the system drive's ratios, any other drive from the data drive's). Load is per core already. Only the
    `wanted` drive letters get a tracker (default: all); a default tracker for R: is switched off when R: is not one.
    A drive already at or under its queue level is warned about, never given lower levels. Returns (trackers, notes)."""
    ts, notes = [], []
    if ram_gb:
        k = ram_gb / REF_RAM_GB
        ts.append({"kind": "ram", "name": "ram", "queue_base_gb": max(2.0, round(6.0 * k, 1)),
                   "hold_gb": max(1.5, round(4.0 * k, 1)), "queue_per_worker_gb": 0.5})
        notes.append(f"RAM {ram_gb:.0f} GB: queue below {ts[-1]['queue_base_gb']:g} GB free, hold below {ts[-1]['hold_gb']:g} GB")
    letters = set()
    for d in drives:
        letter = d["path"][0].upper()
        if wanted is not None and letter not in wanted:
            continue
        letters.add(letter)
        total, q, h = REF_DISKS["system" if letter == "C" else "data"]
        k = d["total_gb"] / total
        queue, hold = max(2, round(q * k)), max(1, round(h * k))
        vhdx = letter in vhdx_letters
        ts.append({"kind": "disk", "name": letter.lower(), "path": d["path"], "wsl_source": "vhdx" if vhdx else "path",
                   "queue_gb": float(max(queue, hold + 1)), "hold_gb": float(hold)})
        notes.append(f"{d['path']} {d['total_gb']:.0f} GB: queue below {ts[-1]['queue_gb']:g} GB free, hold below "
                     f"{ts[-1]['hold_gb']:g} GB" + (" (hosts the WSL disk)" if vhdx else ""))
        if d["free_gb"] <= ts[-1]["queue_gb"]:
            notes.append(f"WARNING: {d['path']} has only {d['free_gb']:.0f} GB free, at or under its queue level: Drover "
                         "would start no new work until you free space there")
    if "H" not in letters:
        ts.append({"kind": "disk", "name": "h", "enabled": False})
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
    a.say("\n6. Resource tracker levels (queue = start no new work, hold = stop everything).")
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


def example_for(name):
    """The shipped example repo of that name (case-insensitive) as a dict, or None."""
    for path in sorted(repos.REPOS_DIR.glob("*.toml")):
        if path.stem.lower() == str(name).lower():
            try:
                with open(path, "rb") as f:
                    return tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError):
                return None
    return None


def repo_toml(p, worktrees_root):
    """Merge repo answers into saved settings, or a shipped example for a new repo."""
    plats = p["platforms"]
    repo = p["repo"]
    trees = (worktrees_root.rstrip("\\/") + ("\\" if "\\" in worktrees_root else "/") + p["name"] + ".worktrees"
             if worktrees_root else repo.rstrip("\\/") + ".worktrees")
    raw = {"name": p["name"], "repo": repo, "worktrees": trees, "default_branch": "main", "remote": "origin",
           "run": {"platforms": plats, "default": "windows" if "windows" in plats else plats[0]},
           "gate": {"platform": p["gate_platform"]}, "land": {"policy": "batch", "push": p["push"]}}
    if p.get("base"):
        base = p["base"]
        updates = {k: raw[k] for k in ("name", "repo", "run", "gate")}
        updates["land"] = {"push": p["push"]}
        if "landing" in base:
            updates["landing"] = {"push": p["push"]}
        if p.get("existing"):
            default = base.get("run", {}).get("default")
            if default in plats:
                updates["run"]["default"] = default
        else:
            updates["worktrees"] = trees
        raw = config._merge(config._merge(raw, base), updates)
    if p["gate_command"]:
        if not p.get("base"):
            raw["gate"].update(timeout_s=1800, idle_s=600)
        raw["gate"]["commands"] = {**raw["gate"].get("commands", {}),
                                   "full": repos.gate_argv(p["gate_command"])}
        # An explicit shared command must take effect on every platform.
        for platform in repos.PLATFORMS:
            raw["gate"]["commands"].get(platform, {}).pop("full", None)
        if p.get("base"):
            raw["gate"]["unknown"] = False
    elif p["gate_command"] is not None:
        raw["gate"]["unknown"] = True
        raw["gate"]["hint"] = "ask the owner: the wizard found no AGENTS.md, test.ps1 or pg.ps1 hint"
    repos.normalize(raw)                 # validates before anything is written
    return config.dumps(raw)


def _saved_repo(state, name):
    state = Path(config.native(state))
    path = _inside(state, state / "repos" / f"{name}.toml")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return None


def q_repos(a, d, found_repos, worktrees, state):
    a.say("\n9. Repos: the repos Drover works on (gate command, platforms, push policy).")
    if "repos" in a.answers:
        out = [_repo_answer(x, state) for x in a.answers["repos"]]
        for p in out:
            repo_toml(p, worktrees)
        _examples_note(a, out)
        return out
    if not found_repos:
        a.say("   no repos found under the repos root; add repos later by re-running init or copying repos/*.toml")
        return []
    for i, r in enumerate(found_repos, 1):
        a.say(f"   {i}. {r['name']}  {r['path']}")
    pick = a.ask("repos_pick", "Repos to set up (numbers, comma separated; all; none)", "none")
    chosen = found_repos if pick.lower() == "all" else [] if pick.lower() in ("none", "") else \
        [found_repos[int(x) - 1] for x in re.findall(r"\d+", pick) if 0 < int(x) <= len(found_repos)]
    out = []
    for r in chosen:
        k = r["name"]
        name = a.ask(f"repo_{k}_name", f"{k}: repo name", k)
        existing = _saved_repo(state, name)
        base = existing if existing is not None else example_for(name)
        repo = a.ask(f"repo_{k}_repo", f"{name}: repo path", (existing or {}).get("repo", r["path"]))
        hint = d["gate_hint"](repo)
        if existing is not None:
            a.say(f"   {name}: keeping saved settings as the defaults")
            cmd = a.ask(f"repo_{k}_gate", f"{name}: gate command (empty = keep saved; or 'ask me later')", "") or None
        elif base:
            a.say(f"   {name}: starting from the shipped example (its gate commands, scope map and review models are kept)")
            cmd = None
        else:
            if hint:
                a.say(f"   gate suggested from {hint[1]}: {hint[0]}")
            cmd = a.ask(f"repo_{k}_gate", f"{name}: gate command (or 'ask me later')", hint[0] if hint else "ask me later")
        while True:
            plats = a.ask(f"repo_{k}_platforms", f"{name}: platforms it may run on (windows,wsl)",
                          ",".join((base or {}).get("run", {}).get("platforms") or []) or d["repo_kind"](repo) or "windows,wsl",
                          _platforms)
            gate_plat = a.ask(f"repo_{k}_gate_platform", f"{name}: gate platform (windows|wsl|both)",
                              (base or {}).get("gate", {}).get("platform") or ("windows" if "windows" in plats else plats[0]),
                              one_of(*repos.GATE_PLATFORMS))
            push = a.ask(f"repo_{k}_push", f"{name}: push landed work to the remote?",
                         bool((base or {}).get("landing", {}).get("push", (base or {}).get("land", {}).get("push", False))), to_bool)
            p = {"name": name, "repo": repo, "platforms": plats, "gate_command": _gate(cmd) if cmd is not None else None,
                 "gate_platform": gate_plat, "push": push, "base": base, "existing": existing is not None}
            try:
                repo_toml(p, worktrees)
                break
            except repos.RepoError as e:
                if a.use_defaults or f"repo_{k}_platforms" in a.answers:
                    raise WizardError(str(e)) from e
                a.say(f"   {e}")
        out.append(p)
    _examples_note(a, out)
    return out


def _examples_note(a, chosen):
    if chosen:
        a.say("   Note: private repo configs live in the state repos/ folder. Packaged examples require DROVER_EXAMPLES=1. "
              "Copy an example there, or re-run init, to add repos.")


def _platforms(v):
    plats = to_list(v)
    bad = [x for x in plats if x not in repos.PLATFORMS]
    if bad or not plats:
        raise ValueError("platforms drawn from windows, wsl")
    return plats


def _gate(cmd):
    cmd = str(cmd or "").strip()
    return "" if cmd.lower() in ("", "ask me later", "later", "unknown") else cmd


def _repo_answer(x, state):
    """Merge answers with the saved repo, using examples only for new repos."""
    existing = _saved_repo(state, x["name"])
    base = existing
    if base is None and not x.get("gate_command") and x.get("from_example", True):
        base = example_for(x["name"])
    plats = _platforms(x.get("platforms") or (base or {}).get("run", {}).get("platforms") or ["windows", "wsl"])
    cmd = _gate(x["gate_command"]) if "gate_command" in x else None if base else ""
    return {"name": str(x["name"]), "repo": str(x.get("repo", (base or {}).get("repo", ""))),
            "platforms": plats, "gate_command": cmd,
            "gate_platform": x.get("gate_platform") or (base or {}).get("gate", {}).get("platform")
            or ("windows" if "windows" in plats else plats[0]),
            "push": bool(x.get("push", (base or {}).get("landing", {}).get("push", (base or {}).get("land", {}).get("push", False)))),
            "base": base, "existing": existing is not None}


# ---- assembling and writing -----------------------------------------------------------------------------------

HEADER = """# Drover configuration, written by `python -m drover init`. Every key is documented in drover.example.toml.
# Keys left out take the built-in defaults. Re-run `python -m drover init` to change it (the old file is backed up).
"""


def _merge_named(existing, updates):
    merged = {x.get("name", x.get("kind")): dict(x) for x in existing}
    for x in updates:
        name = x.get("name", x.get("kind"))
        merged[name] = config._merge(merged.get(name, {}), x)
    return list(merged.values())


def build_config(s, existing=None):
    """The raw config dict for the answers `s`; validated with config.validate before it is returned."""
    existing = existing or {}
    seats = []
    for seat in s["seats"]:
        out = {k: seat[k] for k in ("name", "backend", "priority", "max_workers", "reserve_pct", "director", "placement",
                                    "binary")}
        out["director_reserve_pct"] = seat["director_reserve_pct"]
        if seat["config_dir"]:
            out["config_dir"] = seat["config_dir"]
        if seat.get("codex_home"):
            out["codex_home"] = seat["codex_home"]
        seats.append(out)
    raw = {"paths": {"state_dir": s["state_dir"], "repos_root": s["repos_root"], "worktrees": s["worktrees"],
                     "wsl_distro": s["wsl_distro"], "wsl_user": s["wsl_user"]},
           "seats": seats, "trackers": s["trackers"],
           "decider": {"models": {"primary": s["primary"], "shadow": s["shadow"]}, "device": s["device"],
                       "ollama_models": s["ollama_models"], "commercial_use": s["commercial_use"]},
           "config_sync": {"remote": s["private_remote"], "auto_push": True},
           "hub": {"ui_port": s["ui_port"], "api_port": s["api_port"], "tailscale": s["tailscale"],
                   "allowed_logins": ([s["allowed_login"]] if s["allowed_login"] else [])
                                     + existing.get("hub", {}).get("allowed_logins", [])[1:],
                   "auth": {"mode": "tailscale"}}}
    raw = config._merge(existing, raw)
    config.validate(raw)
    return raw


def _read_existing(state_dir):
    try:
        with open(Path(config.native(state_dir)) / "config.toml", "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _inside(state, path):
    state, path = Path(state).resolve(), Path(path).resolve()
    if state != path and state not in path.parents:
        raise WizardError(f"refusing to write {path}: outside the state dir {state}")
    return path


def write_file(state, path, text, now):
    """Write text to path (inside the state dir only); an existing file is first copied to <name>.bak-<stamp>."""
    path = _inside(state, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now()))}")
        n = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now()))}-{n}")
            n += 1
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return backup


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
