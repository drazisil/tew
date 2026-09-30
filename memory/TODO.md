# TODO

Open items only, a few lines each. Done work goes in changelog.md (one line);
detail lives in git history.

## Perf (remaining after tasks 1-4)
- D3D8 `UnlockRect` -> `_convert_to_bgra8`: ~30% inclusive once textures load.
- `simple_alloc` (`tew/api/_state.py`): free list is never coalesced or
  trimmed, so every alloc walks a growing list.
- sprintf / `_write_cstring` / write8 write byte by byte (use bulk
  write_bytes); `eip`/`eflags`/`get_flag` crossings ~3% each.

## Nested `_invoke_emulated_proc` callers still rewind on step exhaustion
`DispatchMessageA` moved to a stack trampoline (PR #31). Still nested with a
step budget + `restore_state` rewind: timer callbacks (timeSetEvent), message
hooks, CreateDialogParamA's WM_INITDIALOG, CreateWindowEx's creation
messages, `patch_internals.py`, `exception_diagnostics.py`, DllMain calls.
Move any that can run long or block to the trampoline pattern.

## `cpu_add_logpoint` / `cpu_add_breakpoint` silently drop past 8 slots
Fixed `[8]` tables in `cpu/src/core.zig`; a 9th registration is discarded
with no signal. Return a failure and have `cpu_zig.py` raise.

## Thread stacks have no upper bound
`cpu/src/scheduler.zig` bumps each new stack by 256 KB from 0x08000000 and
never reuses them; past ~512 threads they hit the DLL slots at 0x10000000.

## Rendering gaps
- FEUI dialog backgrounds don't draw (Exit dialog shows only its buttons).
- Persona-select highlight bar overdraws the list's column divider. Trace
  the quad's DrawPrimitive before touching blend state; never enable the
  swapchain alpha write mask (the window goes transparent).
- Logo cursor may not animate (low priority; may resolve as a side effect).

## `SetWindowPos` / `MoveWindow` don't touch the SDL window
They update `WindowEntry` bookkeeping and return TRUE; only CreateDevice
resizes the real window. Not seen causing a bug yet.

## Rare `hMutexNfsRunning` assert at ~41s (seen once)
`Nfs_exitCallback` asserts with `_hMutexNfsRunning == NULL`. Unreproduced.
If it recurs: find the CreateMutex site and what starts the exit sequence;
check tew's CreateMutex/CloseHandle.

## Missing opcodes (fix when one shows up)
DAA/DAS/AAA/AAS, MOV Sreg (0x8E), AAM/AAD/SALC, far CALL/JMP/RET, BOUND,
ARPL, INTO, IRET, port I/O, CLI/STI. Any that runs reports
`Unknown opcode: 0xXX at EIP=...`.

## Game debug output (`dprintf` 0x00a34c40) is gated off
Gates: `_winmsgdebugflag` (0x016f3658) >= level; channel byte at
0x01282a1c + channel*2 bit 0; sink table at 0x01282ebc (12-byte entries,
unexamined). Poke them from tew to see the game's own debug text.

## Latent: `bAlertable` ignored in WaitForMultipleObjectsEx / SleepEx
Harmless while no APC source exists (QueueUserAPC, ReadFileEx, WriteFileEx
are unimplemented). Wire it in if any of those are added.

## Test helper: lightweight scheduler mock
For queue/packet tests that only need `current_idx`/thread status.

## Server-side (Molly's), not tew
Lobby headline ticker shows a raw 301 body; "Avg. Player Level: 83,886,080".
