# Status

Target: MCity_d.exe (MSVC debug build, Win32, Pentium II). Reference: Intel
80386 Programmer's Reference Manual, `~/Documents/i386.pdf`.

Current state only; replace it rather than appending. Done work -> one line in
changelog.md. Open work -> TODO.md.

## Current (2026-10-02)

A plain `run_exe.py` plays itself from login to the in-car test drive: Continue
on login, No to full screen, START on persona-select (~93s), CONTINUE on the
Mayor's letter (~151s), Screen Tips X (~182s) and its OK (~212s), the Racing
menu (~243s), its Test Drive item (~273s) and the TEST DRIVE button (~304s);
the cockpit loads by ~550s and runs (lap timer ticks). `TEW_NO_AUTO=1` disables
the clicks; `TEW_MAX_STEPS` is unlimited unless set. The 3D world renders but
badly (see TODO.md Rendering gaps).

**Slow run? Check `ls -la cpu/zig-out/lib/` first:** a ~37MB `libcpu.so` is a
Debug build (`zig build test` reinstalls it); rebuild with
`zig build -Doptimize=ReleaseFast` in `cpu/` (~8.5MB). Debug is ~5x slower and
makes clicks miss.

**Data:** `~/.emu32/Data/GUI` must be the stock GUI set. The game reads loose
`Data/GUI/<name>` files before `System/GUI.viv` (BIGF, 2001-10-21, the
originals), and the click positions assume the stock set.

**Next:** see TODO.md (perf, nested `_invoke_emulated_proc` callers, logpoint
cap).

## Expected in every run
- `dbcode.c(3376) "The class has not been licensed"` -- harmless.
- SEH fault at `EIP=0x004d980f` ~2s in: the game's `_CLayer_DetectDebugger`
  self-test.
- The DAO/Jet startup phase is genuinely slow guest work, not a scheduler
  bug; screens can look like a blocky mosaic while icons load.
