"""Bearer-token auth for the API; Tailscale/loopback auth for the UI."""
import hmac
import json
import logging
import os
import subprocess
import secrets
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException, Request

TS_HEADER = "tailscale-user-login"


BROAD = ("everyone", "authenticated users", "builtin\\users", "nt authority\\interactive")
log = logging.getLogger("hub")


def _icacls(settings):
    if settings.icacls:
        return settings.icacls
    if os.name != "nt":
        return None

    def run(args):
        return subprocess.run(["icacls", *args], capture_output=True, text=True,
                              timeout=30, check=True).stdout
    return run


def owner_name() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def restrict_acl(run, path, is_dir=False) -> bool:
    """Owner-only ACL: drop inherited rights and grant the current user full control."""
    user = owner_name()
    if run is None or not user:
        return False
    grant = f"{user}:(OI)(CI)F" if is_dir else f"{user}:F"
    run([str(path), "/inheritance:r", "/grant:r", grant])
    return True


def acl_warnings(run, path) -> list[str]:
    """Warn when anyone but the owner/SYSTEM/Administrators can touch `path`."""
    if run is None:
        return []
    try:
        out = run([str(path)])
    except Exception as e:
        return [f"could not read the ACL of {path}: {e!r}"]
    bad = [ln.strip() for ln in out.splitlines() if any(b in ln.lower() for b in BROAD)]
    return [f"{path} is readable by a broad group: {ln}" for ln in bad]


def load_token(settings) -> str:
    """Explicit token, else the token file; create it owner-only if missing."""
    if settings.token:
        return settings.token
    path = Path(settings.token_path)
    run = _icacls(settings)
    if not path.exists():
        made_dir = not path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if made_dir:
            restrict_acl(run, path.parent, is_dir=True)
        token = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        restrict_acl(run, path)
        return token
    for w in acl_warnings(run, path) + acl_warnings(run, path.parent):
        log.warning(w)
    return path.read_text(encoding="utf-8").strip()


def real_tailscale(args: list[str]) -> str:
    import subprocess
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            return subprocess.run([exe, *args], capture_output=True, text=True,
                                  timeout=20, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            continue
    return ""


def discover_owner_login(run=real_tailscale) -> tuple[str | None, str]:
    """The machine owner's Tailscale login from `tailscale status --json`.

    Returns (login, why); login is None when it cannot be discovered.
    """
    try:
        data = json.loads(run(["status", "--json"]) or "")
        uid = data["Self"]["UserID"]
        login = data["User"][str(uid)]["LoginName"]
    except (ValueError, KeyError, TypeError) as e:
        return None, f"could not read the owner login from tailscale status: {e!r}"
    except Exception as e:  # runner failure
        return None, f"tailscale unavailable: {e!r}"
    return (login, "discovered") if login else (None, "tailscale reported an empty login")


def discover_tailnet_name(run=real_tailscale) -> str | None:
    """This machine's tailnet HTTPS name (Self.DNSName, no trailing dot)."""
    try:
        name = json.loads(run(["status", "--json"]) or "")["Self"]["DNSName"]
    except Exception:
        return None
    return name.rstrip(".").lower() or None


def resolve_allowed_logins(settings, log=None) -> tuple[str, ...]:
    """Explicit allowlist, else the discovered owner, else () = loopback only."""
    if settings.ui_allowed_logins:
        return tuple(settings.ui_allowed_logins)
    if not settings.use_tailscale:
        if log:
            log.info("hub.tailscale is off: the UI is loopback only")
        return ()
    login, why = discover_owner_login(settings.tailscale or real_tailscale)
    if log:
        if login:
            log.info("UI allowed for Tailscale login %s", login)
        else:
            log.error("remote UI refused (loopback only): %s", why)
    return (login,) if login else ()


def resolve_allowed_hosts(settings) -> frozenset[str]:
    """Host headers a UI request may carry: loopback names on the UI port, the tailnet name."""
    hosts = {f"127.0.0.1:{settings.ui_port}", f"localhost:{settings.ui_port}",
             *(h.lower() for h in settings.ui_allowed_hosts)}
    name = discover_tailnet_name(settings.tailscale or real_tailscale) if settings.use_tailscale else None
    if name:
        hosts |= {name, f"{name}:443"}
    return frozenset(hosts)


def bearer_ok(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, given = header.partition(" ")
    if scheme.lower() != "bearer" or not given:
        return False
    return hmac.compare_digest(given.strip().encode(), token.encode())


def arrived_on(request: Request) -> int | None:
    server = request.scope.get("server")
    return server[1] if server else None


def ui_user(request: Request, settings) -> str | None:
    """Return the UI user name, or None when the request is not a UI request.

    Accepted: a loopback peer (tailscale serve on 127.0.0.1, or a local
    browser) that did not arrive on the API port. A Tailscale login header
    names the user; it is only trusted from a loopback peer. The Host header
    must also be on the allowlist (blocks DNS rebinding and stray callers).

    Known limit: with WSL2 mirrored networking, a WSL process can reach
    127.0.0.1:8765 as a loopback peer; the Host check does not stop it. Keep
    WSL in NAT mode (the default), where WSL only reaches the :8766 API.
    """
    client = request.client.host if request.client else ""
    if client not in settings.trusted_ui_hosts:
        return None
    # DNS rebinding and stray local callers send some other Host
    hosts = getattr(request.app.state, "allowed_hosts", None)
    if hosts is not None and request.headers.get("host", "").lower() not in hosts:
        return None
    port = arrived_on(request)
    if port is not None and port == settings.api_port and port != settings.ui_port:
        return None
    login = request.headers.get(TS_HEADER)
    if login:
        allowed = getattr(request.app.state, "allowed_logins", settings.ui_allowed_logins)
        if not allowed or login not in allowed:
            return None  # no known owner login: remote UI is refused
        return login
    return "local"


SAFE = ("GET", "HEAD", "OPTIONS")


def csrf_ok(request: Request) -> bool:
    """UI mutations must come from our own pages: same-origin fetch metadata,
    or an Origin that matches Host."""
    if request.method in SAFE:
        return True
    site = request.headers.get("sec-fetch-site")
    if site:
        return site == "same-origin"
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    return bool(origin and host and urlparse(origin).netloc == host)


def require_api(request: Request) -> str:
    """Dependency: bearer token, or an authorised UI request. Returns who."""
    state = request.app.state
    if bearer_ok(request, state.token):
        return "token"
    user = ui_user(request, state.settings)
    if user and csrf_ok(request):
        return f"ui:{user}"
    if user:
        raise HTTPException(status_code=403, detail="cross-site request refused")
    raise HTTPException(status_code=401, detail="unauthorized")


def require_ui(request: Request) -> str:
    """Dependency for web routes."""
    user = ui_user(request, request.app.state.settings)
    if not user:
        raise HTTPException(status_code=403, detail="forbidden")
    return user
