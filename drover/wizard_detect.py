"""Read-only machine detection for the setup wizard."""
import json
import os
import shutil
import subprocess
from pathlib import Path

from drover import placement
from . import config
from .tool_discovery import DISCOVERY_SCRIPT, parse_discovery

BENCH_CALLS = 10


def _run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, **placement.NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None


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


def detect_nvidia():
    """{'name', 'used_mb', 'total_mb'} from nvidia-smi, or None."""
    for exe in (["nvidia-smi.exe"] if is_windows() else ["nvidia-smi", "nvidia-smi.exe"]):
        r = _run([exe, "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"])
        if r and r.returncode == 0 and r.stdout.strip():
            try:
                name, used, total = (x.strip() for x in placement.decode_output(r.stdout).splitlines()[0].split(","))
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


CMD_EXE = placement.windows_system_path("cmd.exe")


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
    lines = [x.strip() for x in placement.decode_output(r.stdout).splitlines() if x.strip().lower().endswith((".exe", ".com"))]
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
        r = _run(["bash", "-c", DISCOVERY_SCRIPT], 30)
        found = parse_discovery(placement.decode_output(r.stdout)) if r and r.returncode == 0 else {}
        for t in out:
            out[t]["wsl"] = found.get(t)
    elif distro and any(d["name"] == distro and d["state"] == "Running" for d in placement.detect_distros()):
        probed["wsl"] = True
        r = _run(placement.WSL(distro).command(["bash", "-c", DISCOVERY_SCRIPT]), 30)
        found = parse_discovery(placement.decode_output(r.stdout)) if r and r.returncode == 0 else {}
        for t in out:
            out[t]["wsl"] = found.get(t)
    out["_probed"] = probed
    return out


def detect_config_dirs():
    return {k: os.environ[k] for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME") if os.environ.get(k)}


def _host_ram_wsl():
    """The Windows host's RAM from inside WSL (MemTotal there is only the VM's share), or None."""
    r = _run([placement.windows_system_path("WindowsPowerShell/v1.0/powershell.exe"), "-NoProfile", "-Command",
              "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"], 30)
    try:
        return int(placement.decode_output(r.stdout).strip()) / 1024 ** 3 if r and r.returncode == 0 else None
    except ValueError:
        return None


def detect_ram_gb():
    if not is_windows() and Path(placement.windows_system_path("")).is_dir():
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


def detect_tailscale():
    """{'up': bool, 'login': str|None}."""
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        r = _run([exe, "status", "--json"], 20)
        if r and r.returncode == 0:
            try:
                d = json.loads(placement.decode_output(r.stdout))
                return {"up": d.get("BackendState") == "Running",
                        "login": d["User"][str(d["Self"]["UserID"])]["LoginName"]}
            except (ValueError, KeyError, TypeError):
                return {"up": False, "login": None}
    return {"up": False, "login": None}


def real_detect():
    from . import decide
    return {"windows": is_windows, "home": Path.home, "drives": detect_drives, "free_gb": _free_gb,
            "repo_candidates": repo_candidates, "repos": detect_repos, "worktree_roots": detect_worktree_roots,
            "vhdx_drives": placement.detect_vhdx_drives, "nvidia": detect_nvidia, "ollama": detect_ollama,
            "bench": bench_decider, "ensure_cpu_models": decide.ensure_cpu_models, "tools": detect_tools,
            "config_dirs": detect_config_dirs,
            "ram_gb": detect_ram_gb, "cores": lambda: os.cpu_count() or 1, "wslconfig_cap_gb": detect_wslconfig_cap,
            "distros": placement.detect_distros, "tailscale": detect_tailscale, "exists": lambda p: Path(config.native(p)).exists()}
