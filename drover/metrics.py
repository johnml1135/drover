"""Worker process-tree peaks using /proc or the runner's Windows Job Object."""
import os
import time
from pathlib import Path


class Peaks:
    def __init__(self, read, now=time.monotonic):
        self.read, self.now, self.last = read, now, None
        self.values = {'peak_cpu_pct': None, 'peak_ram_mb': None, 'peak_disk_bytes': None}

    def sample(self):
        try:
            cpu_s, ram_bytes, disk_bytes = self.read()
        except (OSError, ValueError, AttributeError):
            return self.values
        now = self.now()
        if self.last and now > self.last[0]:
            pct = max(0, (cpu_s - self.last[1]) * 100 / (now - self.last[0]))
            self.values['peak_cpu_pct'] = max(self.values['peak_cpu_pct'] or 0, pct)
        self.last = now, cpu_s
        for key, value in (('peak_ram_mb', ram_bytes / 1024 ** 2), ('peak_disk_bytes', disk_bytes)):
            self.values[key] = max(self.values[key] or 0, value)
        return self.values


def proc_group(pgid, root=Path('/proc'), ticks=None, pages=None):
    """CPU seconds, resident bytes and physical IO bytes for this worker's process group."""
    ticks = ticks or os.sysconf('SC_CLK_TCK')
    pages = pages or os.sysconf('SC_PAGE_SIZE')
    cpu = ram = disk = count = 0
    for path in root.iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) != pgid:
                continue
            cpu += (int(fields[11]) + int(fields[12])) / ticks
            ram += int(fields[21]) * pages
            count += 1
            io = dict(line.split(':', 1) for line in (path / 'io').read_text().splitlines())
            disk += int(io.get('read_bytes', 0)) + int(io.get('write_bytes', 0))
        except (OSError, ValueError, IndexError):
            continue
    if not count:
        raise OSError('worker group gone')
    return cpu, ram, disk
