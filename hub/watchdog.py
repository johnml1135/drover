"""Watchdog: boots WSL and keeps it warm while WSL-placed work is due.

The dispatcher runs inside the hub (ADR 0002), so there is no dispatcher to restart. It leaves a `_wsl_needed`
note when it defers a WSL task because WSL is down; this is the only thing that starts WSL. A booted WSL VM idles
out in about a minute, so the watchdog holds it up with a tracked `wsl.exe --exec sleep` child until the note
expires (no WSL work is waiting), then kills that child."""
from drover import placement
import threading
import time
from drover.placement import real_wsl, real_keepalive


class Watchdog:
    def __init__(self, store, settings, bus=None, wsl=real_wsl, now=time.time, keepalive=real_keepalive):
        self.store, self.settings, self.bus = store, settings, bus
        self.wsl, self.now, self._spawn_keepalive, self.keepalive = wsl, now, keepalive, None
        self.started = now()
        self.wsl_failures = 0
        self.last_boot = None
        self._stop = threading.Event()
        self._thread = None

    def _distro(self):
        return placement.distro(self.settings.distro, self.store.get_settings())

    def _log(self, action, message, **data):
        self.store.add_event(f"watchdog.{action}", "watchdog", {"message": message, **data})
        if self.bus:
            self.bus.publish("alert", {"source": "watchdog", "action": action,
                                       "message": message})

    def heartbeat_age(self) -> float:
        hb = self.store.get_heartbeat()
        return self.now() - (hb["ts"] if hb else self.started)

    def wsl_needed(self) -> bool:
        note = self.store._json_setting("_wsl_needed")
        return bool(note) and self.now() - float(note.get("ts", 0)) <= self.settings.wsl_needed_ttl_s

    def keepalive_alive(self) -> bool:
        return self.keepalive is not None and self.keepalive.poll() is None

    def stop_keepalive(self):
        k, self.keepalive = self.keepalive, None
        if k is not None and k.poll() is None:
            try:
                k.kill()
                k.wait(5)
            except Exception:
                pass

    def _hold_up(self, distro):
        try:
            self.keepalive = self._spawn_keepalive(distro, self.settings.keepalive_s)
        except OSError as e:
            self._log("error", f"could not start the WSL keepalive: {e}")

    def check(self) -> str:
        """One decision step. Returns the action taken (or 'ok')."""
        s = self.settings
        if not self.wsl_needed():                       # no WSL work waiting: let WSL idle out
            self.stop_keepalive()
            self.wsl_failures = 0
            return "ok"
        if self.keepalive_alive():
            return "ok"
        distro = self._distro()
        rc, _ = self.wsl(["-d", distro, "-e", "true"], s.wsl_timeout_s)
        if rc == 0:
            self.wsl_failures = 0
            self._hold_up(distro)
            return "keepalive"
        self.wsl_failures += 1
        if self.wsl_failures < 2:
            return "wsl-down-wait"
        if self.last_boot is not None and self.now() - self.last_boot < s.boot_cooldown_s:
            return "boot-cooldown"
        self.last_boot = self.now()
        rc2, out = self.wsl(["-d", distro, "-e", "true"], s.wsl_boot_timeout_s)
        self._log("boot-wsl", f"booted WSL {distro}: rc={rc2}", rc=rc2, output=out[:300])
        if rc2 == 0:
            self.wsl_failures = 0
            self._hold_up(distro)
        return "boot-wsl"

    def _loop(self):
        while not self._stop.wait(self.settings.watchdog_interval_s):
            try:
                self.check()
            except Exception as e:  # never let the thread die
                try:
                    self._log("error", f"watchdog error: {e}")
                except Exception:
                    pass

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="watchdog", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.stop_keepalive()
