"""Run the hub: UI on 127.0.0.1:<hub.ui_port, 8765>, the dispatcher thread, and (only when api_listen_wsl is on) the
API on the WSL adapter at :<hub.api_port, 8766>. Paths and ports come from drover.config."""
import asyncio
import contextlib
import logging
import os
import subprocess
import sys

import uvicorn

from hub.app import create_app
from hub.settings import Settings

log = logging.getLogger("hub")


def discover_wsl_ip(adapter_glob: str) -> str | None:
    """IPv4 of the vEthernet (WSL*) adapter, or None."""
    # the glob travels in the environment, never inside the script text
    ps = ("(Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias $env:DROVER_ADAPTER "
          "| Select-Object -First 1 -ExpandProperty IPAddress)")
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=30,
                             env={**os.environ, "DROVER_ADAPTER": adapter_glob}).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out if out and out != "0.0.0.0" and not out.startswith("127.") else None


def wsl_listener_enabled(app, settings: Settings) -> bool:
    """api_listen_wsl: the Settings field, overridden by the ledger setting of the same name when present."""
    try:
        v = app.state.store.get_settings().get("api_listen_wsl")
    except AttributeError:
        v = None
    if v is None:
        return bool(settings.api_listen_wsl)
    return str(v).strip().lower() in ("1", "true", "yes", "on")


async def serve(app, settings: Settings, discover=discover_wsl_ip):
    ui = uvicorn.Server(uvicorn.Config(app, host=settings.ui_host, port=settings.ui_port,
                                       log_level="info", lifespan="off"))
    tasks = [asyncio.create_task(ui.serve())]
    if wsl_listener_enabled(app, settings):
        tasks.append(asyncio.create_task(serve_api(app, settings, discover)))
    await asyncio.gather(*tasks)


def _announce(app, message, **data):
    """Log, and when the app is a real hub app record a ledger event and publish an alert."""
    log.info(message)
    state = getattr(app, "state", None)
    try:
        state.store.add_event("hub.api-listener", "hub", {"message": message, **data})
        state.bus.publish("alert", {"source": "hub", "message": message})
    except AttributeError:
        pass


async def run_listener(app, server):
    """Run one API listener; a failed bind (uvicorn calls sys.exit) must never kill the hub."""
    try:
        await server.serve()
        return True
    except asyncio.CancelledError:
        raise
    except BaseException as e:  # includes SystemExit from a failed bind
        _announce(app, f"API listener failed ({type(e).__name__}: {e}); will retry",
                  error=repr(e))
        return False


async def serve_api(app, settings: Settings, discover=discover_wsl_ip):
    """Keep the API bound to the current WSL adapter IP.

    Checks every adapter_retry_s: binds when the adapter appears, and on an IP
    change (WSL restarted) stops the old listener gracefully and rebinds.
    """
    ip, server, task = None, None, None
    try:
        while True:
            new = await asyncio.to_thread(discover, settings.wsl_adapter_glob)
            if task is not None and task.done():  # listener died on its own
                await asyncio.gather(task, return_exceptions=True)
                ip, server, task = None, None, None
            if new != ip:
                if server is not None:
                    server.should_exit = True
                    await asyncio.gather(task, return_exceptions=True)
                    server = task = None
                if new:
                    server = uvicorn.Server(uvicorn.Config(
                        app, host=new, port=settings.api_port, log_level="info", lifespan="off"))
                    server.capture_signals = contextlib.nullcontext  # UI server owns signals
                    task = asyncio.create_task(run_listener(app, server))
                    _announce(app, f"API listening on {new}:{settings.api_port}"
                              + (f" (was {ip})" if ip else ""), ip=new, old=ip)
                else:
                    _announce(app, f"WSL adapter {ip} is gone; API listener stopped", old=ip)
                ip = new
            elif not new:
                log.warning("no WSL adapter yet; retrying in %ss", settings.adapter_retry_s)
            await asyncio.sleep(settings.adapter_retry_s)
    finally:
        if server is not None:
            server.should_exit = True
            await asyncio.gather(task, return_exceptions=True)


def main(argv=None):
    logging.basicConfig(level=logging.INFO)
    settings = Settings(run_dispatcher=True, run_services=True)
    app = create_app(settings.ledger_path, settings)
    app.state.start_background()
    try:
        asyncio.run(serve(app, settings))
    finally:
        app.state.shutdown()


if __name__ == "__main__":
    sys.exit(main())
