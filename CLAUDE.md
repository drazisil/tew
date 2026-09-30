# tew — x86/Win32 emulator (Python + Zig CPU core)

<!-- AUTO-COMPACTOR NOTE: discard code blocks, hex dumps and raw file contents; keep the current blocker, the last user directive and any stop-point instructions. -->

Goal: a correct Windows XP environment (OS, CRT, Win32, hardware) rebuilt from
scratch. It must be correct for any well-formed Win32 binary; `MCity_d.exe` is
the test binary, not the target. GPL-3.0; `cpu/` (tew-cpu submodule) is
LGPL-3.0-or-later so other projects can reuse it.

Notes: `memory/status.md` (current state), `memory/TODO.md` (open work),
`memory/changelog.md` (one line per fix). Keep them short; git is the archive.

## Rules

- **One task per session.** State it and the plan; wait for confirmation.
- **Correct halt beats wrong continuation.** Step count is not progress. If
  the game asks something we can't answer truthfully, halt loudly; never fake
  a value to get past a blocker.
- **Handlers implement the API spec**, not what this binary seems to need.
  Before writing or changing a handler, state in chat: function + signature,
  calling convention and arg bytes, what the spec requires, what we deliver,
  and whether that is truthful. If not truthful, the handler halts loudly
  (`logger.error(...)` + fatal halt). Never return spec-contradicting values,
  fake handles/pointers, wrong struct fields, or silent no-ops. An honest
  spec-defined error code with a warning log is fine.
- **No stubs or silent failures** (global standard).
- **Before calling work done**, grep changed files for
  `TODO|FIXME|FAKE|stub|not implemented|pass$|return None|return 0.*#|return False.*#|return True.*#`
  and account for every hit.
- **Garbage in game output means stop**: `0xCDCDCDCD` (-858993460),
  `0xDDDDDDDD`, `0xFEEEFEEE`, negative sizes in anything the game prints mean a
  handler returned success without filling a struct. Find it first.
- **Probes are temporary**: delete logpoints once their question is answered;
  record the finding as one changelog line. Max 8 logpoints and 8 breakpoints
  (extra registrations are silently dropped).

## Run

```bash
cd /data/Code/tew
timeout 250 .venv/bin/python run_exe.py 2>&1 | tee /tmp/emu.log | tail -5
# focused: env LOG_LEVEL=debug LOG_CATEGORIES=startup|handlers|cpu,thread|fileio,registry
.venv/bin/pytest                    # tests (add --cov=tew for coverage)
```

One run at a time; answer follow-ups from the saved log instead of rerunning.
A run plays itself into the lobby (baked-in clicks, `TEW_NO_AUTO=1` disables);
`TEW_MAX_STEPS` is unlimited unless set. tew-cpu: install with
`zig build -Doptimize=ReleaseFast` in `cpu/` (`zig build test` leaves a slow
Debug build installed).

## Layout

- `run_exe.py`: entry point, handler setup, step loop, baked-in clicks.
- `tew/api/`: Win32/CRT/D3D8 handlers (`*_handlers.py`, `kernel32_*.py`,
  `d3d8/`); `_state.py` holds shared CRT state.
- `tew/loader/`: PE loading, relocations, IAT. `tew/kernel/`: TEB/PEB.
- `cpu/`: Zig x86 core, x87 FPU, scheduler (`libcpu.so`, via
  `tew/hardware/cpu_zig.py`).
- Config: `emulator.json` (exe path, `C:\` -> `~/.emu32/`), `registry.json`.
- Handler files register with `stubs.register_handler(...)` (snake_case).
  `msvcrt_handlers.py` imports `time as _time_module`; don't shadow it.
