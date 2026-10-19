"""Windows child-process flags: hub and runner helpers must never open a console window."""
import os

CREATE_NO_WINDOW = 0x08000000

# Splat into subprocess calls: `subprocess.run(argv, **NO_WINDOW)`. Empty off Windows, where creationflags is invalid.
NO_WINDOW = {'creationflags': CREATE_NO_WINDOW} if os.name == 'nt' else {}
