# Status

Target: MCity_d.exe (MSVC debug build, Win32, Pentium II). Reference: Intel
80386 Programmer's Reference Manual, `~/Documents/i386.pdf`.

Current state only; replace it rather than appending. Done work -> one line in
changelog.md. Open work -> TODO.md.

## Current (2026-09-30)

A plain `run_exe.py` plays itself into a clean lobby and stays there: Continue
on login, No to full screen, START on persona-select (~95s), CONTINUE on the
Mayor's letter (~142s), Screen Tips X (~172s) and its OK (~203s). Runs of 250s
end cleanly in the lobby. `TEW_NO_AUTO=1` disables the clicks; `TEW_MAX_STEPS`
is unlimited unless set. Text, persona stats and the HOME avatar all render.

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
