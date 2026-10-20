$ErrorActionPreference = 'Stop'
$python = if ($IsWindows) { 'python' } else { 'python3' }
$driver = @'
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import threading
import time
import traceback

# Fixtures belong to this suite. Keep their temp directories bounded across crashes.
parent = os.environ.get('DROVER_TEST_TMPDIR')
owned = not parent
if owned:
    cutoff = time.time() - 24 * 60 * 60
    for path in Path(tempfile.gettempdir()).glob('drover-fix-*'):
        try:
            if path.lstat().st_mtime < cutoff:
                if path.is_symlink() or not path.is_dir():
                    path.unlink()
                else:
                    shutil.rmtree(path)
        except OSError:
            pass
    parent = tempfile.mkdtemp(prefix='drover-fix-gate-')
else:
    Path(parent).mkdir(parents=True, exist_ok=True)
os.environ['DROVER_TEST_TMPDIR'] = parent
os.environ.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', GIT_ASKPASS='')

# A Python timer avoids the native watchdog involved in a previous Windows crash.
def dump():
    print('Timeout (0:08:20)! stall diagnostics', file=sys.stderr)
    for thread, frame in sys._current_frames().items():
        print('Thread 0x%x (most recent call last):' % thread, file=sys.stderr)
        traceback.print_stack(frame, file=sys.stderr)
    sys.stderr.flush()

watch = threading.Timer(500, dump)
watch.daemon = True
watch.start()
sys.argv = ['unittest', 'discover', '-s', 'tests', '-t', '.']
try:
    runpy.run_module('unittest', run_name='__main__')
finally:
    watch.cancel()
    if owned:
        shutil.rmtree(parent, ignore_errors=True)
'@
& $python -c $driver
exit $LASTEXITCODE
