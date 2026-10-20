"""Tests never read or write the owner's real ~/.drover: DROVER_HOME points at an empty temp directory, and the
configuration variables a developer may have set are removed."""
import atexit
import os
import shutil
import tempfile

from drover import config

_home = tempfile.mkdtemp(prefix="drover-test-home-")
atexit.register(shutil.rmtree, _home, ignore_errors=True)
os.environ["DROVER_HOME"] = _home
for _name in config.env_names():
    os.environ.pop(_name, None)
config.reset()
