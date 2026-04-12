# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

## Current state
10,765 steps through CRT initialization. Halts at `GetStringTypeW`.

## Current blocker
`GetStringTypeW` — CRT locale init calls it with `CT_CTYPE1` to classify
characters (alpha/digit/space/punct flags per wide character).

**Fix:** Implement a character-type table for CT_CTYPE1 covering ASCII
range 0x00–0x7F. That covers all MCO strings. The table maps each wide
char to a bitmask of CTYPE1 flags (C1_ALPHA, C1_DIGIT, C1_SPACE, etc.).

## Next session goals
1. Implement `GetStringTypeW` (CT_CTYPE1 table, ASCII range)
2. See how many more steps we get and what the next blocker is
3. Stretch: begin D3D8 → Vulkan translation planning

## Graphics direction (decided 2026-04-11)
Translate D3D8 calls directly to Vulkan rather than stubbing them.
Python has Vulkan bindings (vulkan / pyVulkan). Cleaner than WebGL
translation from TypeScript. Prerequisite: get past CRT init first.

## Code hygiene status (as of 2026-04-11)
- ruff configured, zero violations
- SavedCPUState lives in hardware layer (cpu.py), save/restore are CPU methods
- Unused imports cleaned, Python 3.12+ required
- Deferred: split _state.py into DTOs vs runtime; split large handler files
  by concept (heap, file, thread, sync) rather than by DLL name
  See memory/project_tew_refactor_notes.md for details
