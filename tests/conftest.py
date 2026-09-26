"""Root pytest conftest -- runs before any test module in tests/ is collected.

SDL_VIDEODRIVER/SDL_AUDIODRIVER must be forced to "dummy" before the first
`import sdl2` anywhere in the suite, or SDL opens a real window on the
developer's actual display and can grab real mouse/keyboard focus (confirmed
2026-09-17: running a narrow test selection -- e.g. just
test_idirect3d8_refcount.py, which triggers a real D3D8 device/window resize
-- without also collecting one of the handful of test files that happened to
set this themselves via `os.environ.setdefault` locked up the real mouse).
Setting it here, in a conftest.py, guarantees it runs first regardless of
which specific test file(s) or subset gets selected -- pytest always loads
conftest.py before collecting any sibling/child test module, unlike the
previous per-file `os.environ.setdefault` calls, which only helped when
those specific files happened to be collected too.
"""

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
