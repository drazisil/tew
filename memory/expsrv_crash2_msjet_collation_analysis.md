# expsrv.dll / msjet35.dll — Second Crash Analysis (new, distinct from the fixed LoadTypeLibEx bug)

## Session context / tooling caveats (read first)

- Ghidra project `debug_clean` was used, as instructed.
- `msjet35.dll` was **already fully analyzed** in the project — `decompile_function` and
  `get_function_instructions` worked normally and the results below are real decompiler output.
- `expsrv.dll` was **not analyzed** — `list_functions` returns empty, and every attempt to run
  `import_and_analyze` on `/home/drazisil/.emu32/WINDOWS/System32/expsrv.dll` failed with
  `ghidra.util.exception.FileInUseException: expsrv.dll is in use`, even after switching the
  active program away and back. This is consistent with the program still being checked out by
  another/stuck client (plausibly the parent session whose Ghidra MCP connection was reported
  stuck at the start of this task). I do **not** have filesystem access outside
  `/data/Code/tew`, so I could not inspect or clear a Ghidra project lock file directly.
- Because of that, **all expsrv.dll findings below are from manual, byte-by-byte x86
  disassembly** (via `dump_bytes`), not Ghidra's decompiler/disassembler. I hand-decoded the
  opcodes carefully and cross-checked instruction lengths against the given return addresses
  (each candidate CALL's byte length lands exactly on the reported return address, which is a
  good consistency check that the decode is correct). Function *names* for expsrv.dll are
  unknown (no symbols, no string table available without analysis), so identification of the
  exact Win32/OLE API involved is **not confirmed** — flagged explicitly below.

## Summary of conclusion

I found a **strong, well-evidenced candidate** for the crash in `msjet35.dll`, directly
anchored by the one EBP-chain-confirmed real stack frame given in the task. I also found two
candidate indirect-call sites in `expsrv.dll` from the (unconfirmed, not EBP-verified) raw stack
addresses; one of them is a plausible match to the "unchecked cached function pointer" bug shape
and the other turned out to be a **red herring** (the code already null-guards it). I could not
fully connect the expsrv.dll and msjet35.dll evidence into one single proven call chain — see
"What remains open" at the end.

---

## 1. msjet35.dll — `FUN_7a87ba0a` @ 0x7a87ba0a (contains the EBP-verified frame, 0x7a87bc04)

This is the **strongest finding** in this analysis, because it is anchored by the task's most
reliable data point: "the one confirmed real EBP-chain frame's return address," 0x7a87bc04.

`get_function_instructions` on `FUN_7a87ba0a` shows the instruction at 0x7a87bc01 is
`CALL COMPUTED_CALL`, and the very next instruction is at 0x7a87bc04 (`MOVZX`) — i.e. **0x7a87bc04
is exactly the return address of an indirect ("computed") call**, matching the EBP-chain value
given.

Decompiled C for that call site (from `decompile_function`):

```c
uint FUN_7a87ba0a(int param_1,uint *param_2,uint *param_3,int param_4)
{
    ...
    piVar11 = *(int **)(param_1 * 0x708 + 0x2c0 + DAT_7a9362c0);
    uVar8 = (**(code **)(*piVar11 + 0x18))(piVar11,&local_28,local_10,param_4);
    ...
```

This is a general-purpose Variant/database-value **comparison** routine — it's called from ~20
different internal locations across msjet35.dll (`get_references_to` on 0x7a87ba0a), consistent
with a low-level "compare two column/variant values" helper used throughout query evaluation,
sorting, and indexing. `param_4 == 0x16` is handled as a special "locale/collation-aware string
compare" path.

**The bug pattern:**
- `DAT_7a9362c0` is a global base pointer to a table of per-session (or per-database-connection)
  structs, each `0x708` bytes.
- `piVar11 = *(struct-at-offset-0x2c0-of-session[param_1])` — a **cached pointer** (almost
  certainly to a COM-style collation/comparison interface object — its dispatch shape,
  `(**(code**)(*piVar11 + 0x18))(...)`, is a vtable call at slot 6) stored per-session.
- The code dereferences `piVar11` (to get `*piVar11`, the object's vtable pointer) and then
  dereferences `*piVar11 + 0x18` (vtable slot 6) — **with no NULL check on `piVar11` and no NULL
  check on `*piVar11`** — before calling through it.
- `DAT_7a9362c0` itself reads as all-zero in Ghidra's static image (`dump_bytes 7a9362c0`), which
  is expected/normal for a runtime-populated global (base pointer, set when the session table is
  allocated). The real question — which I could **not** resolve with the tools/time available —
  is whether the specific per-session slot at offset `0x2c0` (the collation-object cache) was
  ever populated for the session index (`param_1`) in play at crash time.

**Why this matches the crash signature:** if `piVar11` points to a real (allocated, zeroed)
struct whose `0x2c0` slot was never initialized with a real interface pointer, `*piVar11` reads
as `0`. The code then computes `0 + 0x18 = 0x18` as the call target and executes
`CALL [0x18]` — i.e. **EIP jumps to a small, near-zero address**, which in this emulator's flat
zero-filled memory model shows up exactly as "invalid, unmapped, all-zero-bytes" — matching the
crash description precisely. This is the **same general shape** as the already-fixed
LoadTypeLibEx bug (an unchecked call through a lazily-populated cached pointer), but a
**different object entirely**: a per-database-session collation/compare interface cache inside
msjet35.dll, not an OLEAUT32 GetProcAddress cache inside expsrv.dll.

**What I could NOT confirm:** what initializes the `0x2c0` field of a session struct (i.e. what
the real Windows Jet engine calls to set up this collation object — likely something during
`JetOpenDatabase`/`JetBeginSession` when a non-default sort order/collating locale is requested),
and whether the emulator's Jet-session-open path is missing that initialization, versus this
being a legitimate error path in the real DLL that the emulator is reaching via a different,
wrong input. I did not have a `find_field_dispatch_callers`/write-site search budget left to
locate the exact writer of this field with confidence — flagging as open rather than guessing.

---

## 2. msjet35.dll — `FUN_7a888d45` @ 0x7a888d45 (contains "last valid instruction" 0x7a888df0)

Decompiled: this is a **bitmap/free-space-map scan function** (classic Jet "find a set bit in an
allocation extent map" — nested loops over byte/word-granularity bitmap data, shifting and
masking to walk data pages). Full instruction listing confirms 0x7a888df0 is a plain `MOV`
inside this scan loop, several instructions before a clean `RET` at 0x7a888e51. There is
**no indirect call or vtable dispatch anywhere in this function** — it's pure arithmetic/array
indexing over a bitmap, and it returns normally.

I checked whether `FUN_7a87ba0a` (finding #1) calls this function directly — it does not
(`get_function_calls` on `FUN_7a87ba0a` lists ~20 direct callees, none is `FUN_7a888d45`).
So this data point is **very likely incidental** — probably logged as "last instruction executed"
because it ran shortly before the crash as part of unrelated Jet page-allocation bookkeeping
(e.g. allocating a result-set page for the same query), not because it is itself implicated in
the fault. I could not establish a direct causal link between this function and the crash within
the scope of this session.

---

## 3. expsrv.dll — candidate #1 @ static 0x0F9DCDB1 (return address 0x0F9DCDB7) — plausible match

Manual disassembly (`dump_bytes` 0x0F9DCD60–0x0F9DCE00), decoded by hand:

This address sits inside a cluster of small `__stdcall` trampoline functions, each of the shape
"push some stack args → `CALL dword ptr [cached-global]` → `RET N`" — i.e. **thin wrappers around
lazily-resolved (GetProcAddress-style) imported function pointers**, exactly the same
implementation pattern as the already-fixed LoadTypeLibEx/DispCallFunc/UnRegisterTypeLib/
CreateTypeLib2 cache. I found four such wrapper/call sites in this small region alone, calling
through cached slots at 0x0FA0FF70, 0x0FA0FFA8, 0x0FA0FFAC, and 0x0FA0FFB4 respectively.

The one matching the given return address:

```
0f9dcd9c  PUSH ESI
0f9dcd9d  MOV  ESI, [ESP+8]        ; ESI = param 1 (an object/context pointer)
0f9dcda1  PUSH 0x00030001          ; imm32 arg (looks like a packed version: 1.3 or similar)
0f9dcda6  PUSH dword ptr [ESI+8]
0f9dcda9  PUSH dword ptr [ESP+0x14]
0f9dcdad  PUSH dword ptr [ESP+0x14]
0f9dcdb1  CALL dword ptr [0x0FA0FFB4]      ; <-- indirect call, ends at 0f9dcdb7 (matches given return addr)
0f9dcdb7  TEST EAX,EAX
0f9dcdb9  JL   0f9dcdd0                     ; treats EAX as HRESULT-like; jumps on failure
0f9dcdbb  ... (success-path bookkeeping) ...
0f9dcdd0  POP ESI
0f9dcdd1  RET  0xC
```

`dump_bytes` on the cached slot itself:

```
0fa0ffb0  00 00 00 00 00 00 00 00
0fa0ffb8  00 00 00 00 00 00 00 00
```

**The slot at 0x0FA0FFB4 is all-zero** in the static image — exactly the same at-rest signature
the already-fixed LoadTypeLibEx slot had before it was resolved. The call has **no NULL check on
the function pointer itself** before calling through it (only the *return value* of the call is
checked afterward, for a negative/failure HRESULT — that's unrelated to whether the pointer was
valid). If this slot is a GetProcAddress-cache slot (which its position in this cluster of
identical-shape wrapper functions strongly suggests) and the emulator never resolves/writes it,
`CALL dword ptr [0x0FA0FFB4]` becomes `CALL 0x00000000` — a direct match for "jumps to invalid,
near-zero, all-zero-bytes address."

**What I could NOT confirm:** the specific Win32/OLE API this slot represents. `search_strings`
for "TypeLib" returned nothing, and no string table is available at all without running
analysis, so I could not identify the import name the way the earlier (fixed) investigation did.
This is a real gap — I'm flagging it rather than guessing a specific API name.

---

## 4. expsrv.dll — candidate #2 @ static 0x0F9DD03B (return address 0x0F9DD042) — investigated and RULED OUT

Manual disassembly around this address:

```
0f9dd020  CMP  dword ptr [EBP-4],0
0f9dd024  JL   0f9dd057
0f9dd026  CMP  word ptr [EBP+0x14],0x18      ; special-case check: is vt == 0x18 ?
0f9dd02b  JZ   0f9dd044                       ; if so, SKIP the table dispatch below entirely
0f9dd02d  MOVZX EAX, word ptr [EBP+0x14]      ; EAX = vt value (dispatch index)
0f9dd031  LEA  ECX,[EBP-0x14]
0f9dd034  PUSH ECX
0f9dd035  PUSH dword ptr [EBP+0x10]
0f9dd038  PUSH dword ptr [EBP+0xC]
0f9dd03b  CALL dword ptr [EAX*2 + 0x0FA04188]   ; <-- indirect call, ends at 0f9dd042 (matches given return addr)
0f9dd042  ...
0f9dd043  JMP  0f9dd044 (short, "eb 10")         ; success path rejoins here
```

This is a **VARTYPE-indexed dispatch/coercion table**, not a lazily-resolved import cache.
`dump_bytes` on the table (0x0FA04188 onward) shows it is **fully populated with real, in-range,
already-relocated code addresses** (`xx cc/cd 9d 0f` = 0x0F9DCCxx/0x0F9DCDxx, all inside
expsrv.dll's own static image) — this is genuine static compile-time data, not an
uninitialized runtime cache. There is exactly **one null entry** in the table, at offset 0x30
from the table base (0x0FA041B8 = `00 00 00 00`).

Critically: offset 0x30 in a 4-byte-stride, `EAX*2`-scaled table corresponds to index `EAX = 0x18`
— and the code **already explicitly special-cases `vt == 0x18` a few instructions earlier**
(`CMP word ptr [EBP+0x14],0x18` / `JZ`), routing that case around the table dispatch entirely
before it can ever hit the null slot. In other words: **this call site is already correctly
guarded against its one bad table entry in the real code.** This is not the bug — I'm including
it to show it was checked and ruled out, not left unexamined.

---

## What remains open / not fully proven

1. **I have not proven a single continuous call chain** connecting expsrv.dll's cached-pointer
   call (#3) to msjet35.dll's collation-object call (#1). Both are strong *independent*
   candidates matching the crash's "near-zero, unchecked cached pointer" shape, but the task's
   own framing of the stack evidence (only one EBP-chain-*confirmed* frame, at msjet35.dll
   0x7a87bc04; the two expsrv.dll addresses are "real stack values," not stated to be
   EBP-verified) means candidate #1 (msjet35.dll) is the better-anchored of the two, not
   necessarily the exclusive cause. It's plausible the expsrv.dll stack values are older/stale
   data from an earlier call in the same chain (e.g. expsrv calling into Jet's public interface
   to evaluate a VBA expression referencing a database field, which several calls deep reaches
   `FUN_7a87ba0a`'s unchecked vtable call) — but I could not verify the intermediate call chain
   linking expsrv.dll's public entry points down to `FUN_7a87ba0a`, since expsrv.dll has no
   analyzed functions to trace from.
2. **I could not identify the exact Win32/OLE API behind the expsrv.dll 0x0FA0FFB4 slot** (no
   string table without analysis).
3. **I could not locate the write-site that populates the msjet35.dll per-session `0x2c0` field**
   (the collation-object cache in finding #1), so I cannot say definitively whether this is an
   emulator init-ordering bug (a Jet API the emulator doesn't call/implement) vs. the real DLL's
   own legitimate-but-unreached error path.
4. **expsrv.dll could not be re-analyzed in this session** due to a persistent Ghidra file lock
   (`FileInUseException`) that did not clear even after switching programs. If a future session
   has a healthy Ghidra connection, running `import_and_analyze` on expsrv.dll and decompiling
   the two candidate wrapper functions properly (with real function names/import resolution)
   would very likely resolve open items #1 and #2 above.

## Recommendation

Given the EBP-chain anchor, **investigate msjet35.dll's `FUN_7a87ba0a` (0x7a87ba0a) first**:
check what the emulator does (or fails to do) to populate the per-session struct field at
`param_1*0x708 + 0x2c0` relative to the base at `DAT_7a9362c0` (static 0x7a9362c0) before any
query/comparison runs. If time allows, also re-run Ghidra analysis on expsrv.dll once its file
lock clears, to properly identify the 0x0FA0FFB4 cached-pointer slot and confirm/rule out
candidate #3 as part of the same chain.
