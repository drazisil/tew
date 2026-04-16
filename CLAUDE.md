# tew — x86/Win32 Emulator (Python port)

<!-- AUTO-COMPACTOR NOTE: When compressing conversation history, discard large code blocks, hex dumps, binary data listings, and raw file contents. Summarize what was found/done instead. Preserve: current blocker, last user directive, any explicit stop-point instructions. -->

## Project Goal

This emulator's goal is to see how far we can get rebuilding a correct Windows XP
environment — OS, CRT, Win32 API, and hardware — from scratch in Python.

The emulator must work correctly for any well-formed Win32 binary — not just
`MCity_d.exe`. If a handler would break on a retail build, an early build, or a
different program entirely, it is wrong. `MCity_d.exe` is the test binary because
it's well-understood and exercises a wide Win32 surface area. Rusty Motors is a
downstream use case, not the driver.

## Session Discipline (REQUIRED)

**Before writing any code, state the single task for this session.**
Rich context does not imply permission to act on all of it.
One task. Show your plan. Wait for confirmation before touching anything.

**Step count is not a success metric.** A correct halt at step 100 is better than
6 million steps built on handlers that returned garbage. Do not report "the emulator
ran further" as progress if the additional steps were on top of incorrect output.
The goal is a correct Win32 environment, not a high step count.

**A correct halt is more progress than a wrong continuation.** If the game is asking
us something we cannot truthfully answer, halt loudly. Do not fake a value to get
past the blocker and keep running — that pushes the real problem deeper and makes it
harder to find.

---

## HARD RULE: Handler Correctness (MANDATORY)

Every handler must implement what the Win32/CRT/D3D8 API spec says that function does.
**The spec is the only criterion.** "The game probably doesn't use this return value"
is not a justification — Ghidra's decompilation is imperfect, and we are not building
for one binary. A handler that is correct for MCity_d.exe but wrong for a retail or
early build is a broken handler.

**Before writing or modifying ANY handler, state in chat:**

```
HANDLER DECLARATION
Function : <name>(<args>) -> <return type>
Signature: stdcall|cdecl, <N> arg bytes cleaned by callee|caller
Spec says: <what the API contract requires — from MSDN or equivalent>
We deliver: <what we actually implement>
Truthful : YES — <reason> | NO — must halt loudly instead
```

Do not include "game use" or "what the binary does with this" as a field. That is
not a criterion. The spec is.

If "Truthful: NO", the handler body MUST be a loud halt:
```python
logger.error('handlers', '[UNIMPLEMENTED] <name> — halting')
cpu.halted = True
return
```

**No handler may:**
- Return a value that contradicts the API spec
- Write values into a struct that don't match the spec's field definitions
- Return a fake handle/pointer that implies a capability we cannot back
- Silently no-op — halt loudly or return the correct spec-defined error code with a warn log

---

## HARD RULE: No Stubs, No Silent Failures

Matches global CLAUDE.md standard. Do not write `pass`, `return 0  # TODO`,
or any body that pretends to work. If a handler cannot be completed in scope, say
so in chat. Incomplete-but-honest is better than complete-but-fake.

---

## Post-Implementation Audit (REQUIRED before declaring work done)

"Done" means the whole integrated thing is correct — not that the new handler
compiles, not that the emulator runs further. After adding any handler, module, or
call chain, verify that every piece you touched produces spec-correct output before
moving to the next blocker.

Before reporting completion, search every changed file for these patterns using the
Grep tool:

```
TODO|FIXME|FAKE|stub|not implemented|pass$|return None|return 0.*#|return False.*#|return True.*#
```

For each match: is this intentional and honest (e.g. a real spec-defined return of 0),
or is it concealing an unimplemented capability? If concealing — fix it or halt loudly.
Do not ship a session that passes this grep without explaining every hit.

The purpose: once implementation starts, everything gets viewed through the lens of
"does this matter for what I'm building right now." This grep breaks that lens.

---

## Warning Signs — Do Not Proceed Past These

**`0xCDCDCDCD` in output** (decimal: `-858993460`): MSVC debug heap uninitialized
memory fill pattern. If a game OutputDebugString shows this in a struct field (display
width, height, frequency, etc.), a handler returned success but never filled the
struct. The game is running on garbage. Find the handler and fix it before continuing.

**OutputDebugString output is diagnostic signal, not noise.** When the game prints
something, read it. The game is telling you what it found. `-858993460` in a video
mode struct means the display enumeration handler lied. Do not filter it or run past it.

**Nonsensical values in any game-printed struct** (negative dimensions, 0xCDCD...,
0xDDDDDDDD, 0xFEEEFEEE): stop and find the handler that produced them. Do not
continue past known garbage output chasing a higher step count.

---

## Context Optimization Rules

- **Do not re-read files** already read this session unless content may have changed.
- **Do not rerun the emulator** to answer a follow-up question — use `/tmp/emu.log`.
- **Keep searches targeted**: specific file paths and known addresses, not broad sweeps.
- **Current status lives in memory**: check `~/.claude/projects/-home-drazisil/memory/project_emulator.md`.
  Do not trust status written inline in skill files — it goes stale.

---

## How to Run

```bash
cd /data/Code/tew

# Standard run (save full log — always):
timeout 60 .venv/bin/python run_exe.py 2>&1 | tee /tmp/emu.log | tail -5

# Focused runs:
timeout 60 env LOG_LEVEL=debug LOG_CATEGORIES=handlers,startup .venv/bin/python run_exe.py 2>&1 | tee /tmp/emu.log | tail -5
timeout 60 env LOG_LEVEL=debug LOG_CATEGORIES=cpu,thread .venv/bin/python run_exe.py 2>&1 | tee /tmp/emu.log | tail -5
timeout 60 env LOG_LEVEL=debug LOG_CATEGORIES=fileio,registry .venv/bin/python run_exe.py 2>&1 | tee /tmp/emu.log | tail -5
```

**Run ONCE per session. All follow-up analysis greps `/tmp/emu.log`. Never re-run to answer a question.**

---

## Project Structure

```
run_exe.py                          — Entry point: handler setup, validRanges, step loop
tew/
  api/
    kernel32_handlers.py            — Process/system/heap/thread/scheduler handlers
    kernel32_io.py                  — File I/O, sync objects, time, misc handlers
    msvcrt_handlers.py              — CRT: malloc/free/string/file/time
    user32_handlers.py              — User32/GDI32 handlers
    oleaut32_handlers.py            — OleAut32/Ole32 handlers
    advapi32_handlers.py            — Advapi32 handlers
    d3d8_handlers.py                — Direct3D8 COM handlers
    wininet_handlers.py             — WinInet handlers
    version_handlers.py             — Version API handlers
    crt_handlers.py                 — Orchestrates handler registration; returns CRTState
    _state.py                       — CRTState shared mutable state (heap/files/threads/TLS/registry)
    win32_handlers.py               — Win32Handlers class + INT 0xFE dispatch
    patch_internals.py              — CRT internal function patches (post-load)
    char_type.py, lc_map.py         — Pure-logic modules
    win32_errors.py, ini_file.py    — Pure-logic modules
  loader/
    dll_loader.py                   — PE loading + base relocations
    import_resolver.py              — IAT population
  hardware/
    cpu.py                          — x86 CPU core
    memory.py                       — Flat bytearray memory
```

---

## Memory Layout

```
0x00200000 – 0x21FFFF   Win32 API trampolines (INT 0xFE, 32 bytes each)
0x00210000+             Fixed data region (handler index 2048+)
0x001FE000              Thread sentinel (INT 0xFE; RET)
0x00300000              PEB
0x00320000              TEB
0x00400000+             PE image (MCity_d.exe)
0x04000000+             Heap (bump allocator)
0x08000000+             Cooperative thread stacks
0x40000000+             VirtualAlloc region
```

---

## Python Pitfalls

- Methods are snake_case: `register_handler`, `patch_address`, `get_handler_address`
- `patch_crt_internals(stubs, mem, state)` requires the `CRTState` returned by `register_crt_handlers`
- `import time as _time_module` in `msvcrt_handlers.py` — must NOT shadow with `def _time(cpu)`
- Handler files use `stubs.register_handler(...)` — not `registerHandler`

---

## Config Files

- `emulator.json` — exe path, path mappings (`C:\` → `/home/drazisil/.emu32/`), interactiveOnMissingFile
- `registry.json` — fake Win32 registry values for MCO install keys
