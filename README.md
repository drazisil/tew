# tew

An x86-32 emulator that aims to rebuild a correct Windows XP-era environment
(OS, C runtime, Win32 API, and hardware) from scratch. The main test binary is
`MCity_d.exe`, the MSVC debug build of Motor City Online, chosen because it
exercises a wide slice of the Win32 surface.

## Emulated CPU

A **Pentium II-class (P6) core**: `CPUID` reports family 6 with FPU, CMOV and
MMX; `SYSENTER`/`SYSEXIT` are implemented; there is no SSE. (A strict Pentium
Pro would not advertise MMX.) The instruction core is Zig (`cpu/`, a git
submodule built as `libcpu.so`) and is driven from Python through ctypes.

Unimplemented opcodes halt the run and are reported as
`Unknown opcode: 0xNN at EIP=0x...` (the address of the instruction that failed
to decode).

## Running

```
cd /data/Code/tew
.venv/bin/python run_exe.py
```

Useful environment variables include `TEW_MAX_STEPS`, `LOG_LEVEL` and
`LOG_CATEGORIES`. For scripted clicks, `TEW_CLICK_AT=x,y` together with
`TEW_CLICK_WHEN_FILE`/`TEW_CLICK_WHEN_TEXT` clicks once the game writes a given
line to a log file, and a `/tmp/tew_click_trigger` file drives a click on demand.

## Tests

```
zig build test --build-file cpu/build.zig
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy .venv/bin/python -m pytest tests
```

Run pytest from the repository root, and keep the SDL dummy drivers set: without
them a narrow test selection can open a real window and grab your mouse.
`tests/unit/api/test_dinput_handlers.py` allocates heavily per test; on a
machine short on memory run the rest of the suite with
`--ignore=tests/unit/api/test_dinput_handlers.py`.

## Notes for contributors

x87 arithmetic runs on the host's real x87 unit (Zig `f80`). Never discard an
`f80`-returning function's result (`_ = f()`): the value is left on the host x87
stack and leaks an entry per call, which eventually turns arithmetic into NaN.
Project history and current status live in `memory/` (`status.md`, `TODO.md`,
`changelog.md`).
