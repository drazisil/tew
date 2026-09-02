# Heap sizing & message-object memory in tew — research notes

*Living reference doc, not an execution plan. Intent: walk through this together, slowly, revising as we go — not a checklist to run through.*

## Why this exists

`state.simple_alloc()` (`tew/api/_state.py:513-528`) is a pure bump-pointer allocator backing a 64MB guest heap (`0x04000000`-`0x08000000`, ceiling `THREAD_STACK_BASE` at `_state.py:319`). It tracks every allocation's size in `heap_alloc_sizes[addr]`, but nothing anywhere reclaims that space, and a real run already hit the ceiling once (collided with the thread-stack region). Before deciding what to do about it, we're mapping out where the real game's memory actually goes — both tew's own known gaps, and (this doc's real subject) the real MCity_d.exe's various internal allocators/pools, since "zillions of objects" needs a real shape before any sizing decision means anything.

## tew's own known gaps (confirmed via code, not decompile)

- **No reclamation mechanism exists anywhere** in tew's codebase (grep for freelist/pool/arena/reclaim: zero hits). `simple_alloc` only ever moves its cursor forward.
- **A second, independent bump allocator** (`_heap_alloc`/`_next_heap_addr` in `tew/api/d3d8/_helpers.py:54-59`, starting at `0x04800000`) shares the *same* address range as `simple_alloc` with zero coordination — a latent collision. `make_vtable(stubs, memory)` in all four d3d8 interface files never receives `state`, which is why this allocator exists standalone.
- **10 confirmed gap sites** allocate via `simple_alloc` repeatedly but never free, free only partial bookkeeping, or are hardcoded no-ops. Table of these is below, kept for later reference — not urgent given the finding below.

## The real game's memory picture (from direct Ghidra decompile of MCity_d.exe, this session)

### 1. `_MEM_*` — the engine's own allocator, one big arena

`_REAL_init` (`0x00a34980`) unconditionally calls `_MEM_init` (`0x00a719e0`) → `_MEM_initsize(0x4000000)` (`0x00a71990`) → **`_MEMlowadr = malloc(0x4000000)`** — one 64MB request, exactly the size of tew's *entire* current guest heap region.

After that single grab, `_MEM_alloc`/`_MEM_free` (`0x00a70520`-`0x00a72860`) run a real boundary-tag, coalescing, per-size-class allocator *entirely inside guest memory the game manages itself*. Every "zillions of objects" allocation the engine makes through this path lives inside that one 64MB block — invisible to `heap_alloc_sizes`/`simple_alloc` after the initial `malloc`.

**Correction (see §9 below)**: this was this doc's original theory for the `THREAD_STACK_BASE` collision hit earlier this session, reasoned from decompile alone. Direct log/crash-dump evidence gathered afterward shows that's **not** what actually happened in the run that was captured — the real exhausting allocation was a routine 2040-byte `HeapAlloc`, and the heap had been filled by something else entirely (real DAO/COM query traffic, not `_MEM_init`). `_MEM_*`'s single 64MB requirement is still real and still worth accounting for in any future sizing decision — it just isn't the mechanism behind the one confirmed crash. Don't treat the crash as proof of this section; treat this section as an independent, still-true fact about the engine's own memory shape.

### 2. `DBMem_*` — the DAO/Jet database's own pool, and what TCPManager actually is

`DBMem_Init` (`0x00925d30`) does *not* use `_MEM_*`. It builds a C++ `MemoryPool` via ordinary `operator_new`→`malloc` and calls `MemoryPool::Initialize(pool, 0x800, 0x28)` — but that `0x800`/`0x28` is the pool's own *internal bookkeeping* allocator (fixed-size internal blocks), **not** a cap on individual `DBMem_Alloc` request sizes. It draws from the *same* shared `simple_alloc` region as everything else.

**Corrected via real log evidence** (`~/.emu32/dblog.txt` from a run this morning) — actual `DBMem_Alloc` sizes observed in just the first few seconds of database activity: `7528`, `23200`, `87324`, `24`, and `360900` bytes. Real usage is much larger and more variable per-request than the ~80KB figure implied — that number was never a footprint estimate, just a misreading of the pool's own internal block size.

**TCPManager, per direct domain knowledge**: `TCPMgr` is strictly the MCOS/Transactions/`*Database` layer — attached to `gNPS`/`gINET`, it's what manages the `MessageNode`s, and it's the exclusive domain of the DB thread once `dblog.txt` shows the switch to online mode. Confirmed in the same log: `dbcode.c(1304) Dbcode_StartSingleRaceAndOnlineMode` is the literal online-mode transition, immediately followed by a numbered `DBServiceRequestQ` ticket queue — `DBHandlers.c(1325) DBServiceRequestQ: request #704 DBT_GO_SINGLERACE`, `#705 DBT_STARTUP`, `#763 DBT_GET_GAMECONFIG_CAR_TABLE`, `#764 DBT_GET_SKIN_ID_LIST`, `#708 DBT_GET_STOCK_CAR_LIST`. This is almost certainly where `MessageNode`/`MessagePool` actually gets exercised in practice: each numbered DB request/response is a transaction routed through TCPMgr's message-passing machinery on the DB thread, even in offline "SingleRace" mode — MCO's client talks to its own local/embedded transaction handler the same way it would talk to the real online service.

### 3. `MessagePool` — a real, bounded, *visible* object pool (the network message layer)

Checked `KList<MessageNode*, MessageNode*>` (`0x008ce8c0`) first — it's just the generic list container: constructor sets up a mutex + head/tail/count, no pool logic, and `Add`/`Remove` are non-templated shared code over `void*`. The actual pool is a separate class wrapping it:

`MessagePool` (ctor `0x00a821ae`, dtor `0x00a822dd`, `GetPoolStatus` `0x00a82d4a`) — real preallocation logic lives in `MessagePool::Initialize(this, msgSize, maxCount, owner)` at `0x00a82213`:

```c
for (i = 0; i < maxCount; i++) {
    node = operator_new(msgSize + 0x18);   // one small heap alloc per node (24-byte header + payload)
    memset(node, 0xDB, msgSize + 0x18);    // MSVC debug "uninitialized" fill pattern
    KList<MessageNode*,...>::Push(this, node);   // pushed onto the pool's free list
}
```

This is a genuine object-pool pattern, but built from **`maxCount` separate small `operator new` calls** — unlike `_MEM_*`, every node here is individually tracked, ordinary CRT heap allocation, fully visible to `simple_alloc`/`heap_alloc_sizes`.

`TCPMgr::Initialize` (`0x00a7a4fe`) constructs **three** `MessagePool` instances (one per message-priority class, stored at `this+0x34/0x38/0x3c`), with `msgSize`/`maxCount` sourced from the caller's `SocketInitializer` config — real, bounded, config-driven numbers (not yet read live from a running instance). This preallocation happens at `TCPMgr::Initialize`, in the same startup window as `_MEM_init` — a real, additive contributor to *ordinary* heap traffic, on top of CRT/Win32/COM/`DBMem_*` traffic, separate from and much smaller than the 64MB `_MEM_*` arena.

Same function also does a genuine array-new of `local_118` `KList<MessageNode*>` objects (`operator_new(local_118 * 0x1c + 4)`, classic MSVC array-new with a leading count header) — likely one queue per connection slot, sized by a runtime `SocketInitializer` field (`param_1[2]`), not a huge fixed constant.

### 4. `MessageQueue` — false lead, ruled out; `cQ` is the real thing

Decompiled `MessageQueue` (`0x00bb5ed0`) first: it's `CryptoPP::MessageQueue` (Crypto++'s `ByteQueue`/`BufferedTransformation` machinery), used for crypto buffering. Nothing to do with NPS message passing — same name, unrelated subsystem. (Same trap as `MTicker::AddMessage` earlier — this binary reuses generic names across unrelated subsystems more than once; worth remembering as a standing caveat when searching by name alone.)

The actual generic queue underlying most of the game's linked-list-of-objects pattern is **`cQ`** (ctor `0x00502350`, dtor `0x00502490`). Its node type, `tsQnode`, is a plain 20-byte doubly-linked node (`prev`/`next` pointers plus two more int fields). The ctor takes an optional `tsQinfo*` (16 bytes) that can request a critical section for thread-safe use, but does **no preallocation** — same shape as `KList`: just a list header, nodes added later one at a time.

`cQ` turns out to be the real backbone of a huge share of the NPS object graph — real callers (via the `cQ` construction thunk at `0x004019ba`) include: `cNPS_UserList`, `cNPS_ChatList`, `cNPS_DirectChat`, `cNPS_RoomInfoList`, `cContactList`, `cNPSC_Persona`, `cNPSC_Mail`, `cUsers`, `cFilterList`, `cConnQ`, `cCompletedQ`, `INet_AsyncOps`/`INet_AsyncOps_Executer`, `Chat_OpenHailingFrequency`, `Port_Checker`, `Chat_Filter` — and, most directly relevant, **`cSendMsgQ`** (`0x00ac9c10`) and **`cReadMsgQ`** (`0x00acbda0`), which are almost certainly the real send/receive message queues for the NPS networking layer (the plausible actual home of `gNPS`'s `DeliverMessageFunc`/`NPSThreadSender` traffic, more so than the `KList<MessageNode*>` container looked at above).

**Implication**: since `cQ` preallocates nothing, every object queued through any of these (`cNPS_User`, chat messages, contacts, personas, mail, connections, completed async ops, send/read messages) is an individual, ordinary heap allocation at the moment it's queued — this is very likely the real machinery behind "zillions of objects," much more so than `MessageNode`/`KList`. Worth treating `cSendMsgQ`/`cReadMsgQ` as the priority follow-up over the `KList<MessageNode*>` angle.

### 5. `NPSMessageContainer`/`NPSMessageContainerGC` — per-message objects, no pool

Both `NPSMessageContainer::NPSMessageContainer` (`0x00aab880`) and its GC-tracked subclass `NPSMessageContainerGC` (`0x00aab820`) are small, individually-constructed wrapper objects — each ctor calls `createMessage(this, ...)` to build the message content. No batch/pool allocation in either constructor; these are created one at a time, presumably per outgoing/incoming NPS message, not preallocated in bulk like `MessagePool`.

### 6. `NPSLoginAPI` — cheap at init, no message-object cost upfront

`_NPSLoginAPI_GetInterface` (`0x00aa29d0`, the `gNPSLogin` singleton, login's analog to `gNPS`) is trivial: lazy-inits `gNPSLogin` with one `operator_new(4)` (an empty vtable-only `cNPSLoginAPI` object) and calls `Auth_LoadDll(...)`. No `MessagePool`, no `NPSMessageContainer` batch, nothing heap-heavy happens here. Whatever message containers login eventually creates are per-event (e.g. per login attempt), not part of `gNPSLogin`'s construction.

### 7. Real runtime evidence — the actual major actors, from a live run's `OutputDebugStringA` capture

This morning's run (`/tmp/emu.log`) captured real `OutputDebugStringA` text — tew logs every intercepted call, giving a ground-truth sequence rather than a decompile guess. Within the first ~2.8 seconds of the run:

```
1.507s  Created Chat Filter thread, Handle = 0xBEEF
1.509s  Filter thread started, Handle = 0xBEEF : ID = 0x3E9
2.735s  Created an INet thread, Handle = 0xBEF0
2.737s  INet thread started, Handle = 0xBEF0 : ID = 0x3EA
2.739s  AnalyzeAPI Initialized
2.744s  Created an INet thread, Handle = 0xBEF1
2.746s  INet thread started, Handle = 0xBEF1 : ID = 0x3EB
2.747s  Creating INET Account Object
2.747s  Creating INET Persona Object
2.748s  Creating INET Contact Object
2.841s  Creating INET Message Object
43.657s --DBThread is alive! (han=0xBEF9  threadid=0x3F3)
```

**The two threads are these two "INet thread" instances** (handles `0xBEF0`/`0xBEF1`) — matching `INet_AsyncOps`/`INet_AsyncOps_Executer`, both confirmed `cQ` callers from the decompile above. Not two separate unrelated threads to go hunting for elsewhere — this is the same pair already known from the `gNPS` construction path, just surfaced here under generic "INet thread" debug-string naming rather than the `DeliverMessageFunc`/`NPSThreadSender` names seen in the decompile.

This also gives a real, ordered list of the major actors constructed at startup: a **Chat Filter thread** first, then the two **INet threads**, then **AnalyzeAPI**, then — in sequence — **Account**, **Persona**, **Contact**, and **Message** objects. The "Creating INET Message Object" line at 2.841s is concrete evidence that message-object construction is an early-startup event (within ~100ms of the Account/Persona/Contact trio), not something deferred until actual gameplay/networking traffic — worth weighing against the "MessagePool preallocates at `TCPMgr::Initialize`" and "cQ-backed lists grow incrementally" pictures above: this sequence suggests at least one message object gets created immediately as part of the same startup burst as Account/Persona/Contact, which fits a `cQ`-backed incremental object (matches §4) more than a `MessagePool` preallocation batch (which would show as `maxCount` repeated identical calls, not one single "Creating INET Message Object" line).

The run itself didn't get further than this — it later halted on the known `dbcode.c(3376) The class has not been licensed` DAO issue (per `stdout.txt`), so this doesn't yet show what happens once real networking/gameplay traffic starts flowing through `cSendMsgQ`/`cReadMsgQ`.

### 8. `cMap<K,V>` — complete inventory, real STL underneath

Full inventory of `cMap` usage in MCity_d.exe: only **two** real instantiations exist.

`cMap<K,V>` itself is a thin wrapper around a genuine `std::map<K, V, std::less<K>, std::allocator<V>>` (MSVC6 STL red-black tree) embedded as a member — its ctor calls `std::map::map()` directly, and its virtual dtor walks the tree calling a per-element virtual `Remove()` hook before destroying the underlying `std::map`. It adds a critical-section wrapper (`InitLock`/`Lock`/`UnLock`/`DeleteLock`) for thread safety. **This is real STL, not a custom pool or arena** — each key/value node is an ordinary, individually-tracked heap allocation via the STL's own allocator, same shape as `cQ`'s incremental growth, not `MessagePool`'s batch preallocation.

The two instantiations:
- **`cMap<long, cNode<cServerData>>`**, owned by **`cServers`** (ctor `0x00ac32b0`) — almost certainly the *same* `cServers`/`ConnectedServers` object (96 bytes) already found earlier this session as part of `gNPS`'s own construction path (`FUN_00ac2b10`). A server registry keyed by server ID (`long`).
- **`cMap<long, cNode<cCommData>>`**, owned by **`cChannels`** (ctor `0x00ad9030`) — a communication-channel registry, same key shape.

Both `cNode<T>` payloads are wrapped in a `cSmartPtr<cNode<T>::sNode_Data>` — a ref-counted smart pointer, not a raw pointer. Node lifetime is refcount-driven, not tied to a single deterministic owner/scope — worth remembering if this ever needs modeling precisely (a node can outlive its container entry if something else holds a `cSmartPtr` to it).

**Ties the linked-list survey together**: `gNPS` → `cServers` (`cMap`-backed server registry) is now a confirmed, named piece of the same object graph as `DeliverMessageFunc`/`NPSThreadSender` (the two `INet` threads from §7) and the `cQ`-backed lists in §4 — all consistent with one connected picture rather than isolated unrelated containers.

**Checked "NPSRoomList" specifically** (no such symbol exists — the real thing is `cNPS_RoomInfoList`, part of `cNPS_RoomServer`) to make sure the `cMap` scarcity wasn't hiding a missed map-backed room index. It isn't: `cNPS_RoomServer`'s real ctor (`0x004ea780`) embeds `cNPS_RoomInfoList` **by value** at `+0x458` (constructed inline, alongside `cNPS_UserList` at `+0x4a0` and `cNPS_ChatList` at `+0x584` — same object). `cNPS_RoomInfoList` (`0x004e49f0`) directly **inherits** from `cQ` (`cQ::cQ(this, NULL)` as its base-class constructor call, not composition) and calls `cQ::Init(this, {nodeSize: 0x15c, ...})` — 348-byte nodes. It also builds a nested `cFindByName` helper (`0x004e4b50`), but that class has **zero data fields** — it just wraps `cForeignFinder` (a vtable-only strategy object) and is used to do a name-based linear scan through the `cQ` list itself, not a separate index. So the room list really is: `cQ`-derived incremental linked list + a name-search callback bolted on, no hidden map. This reinforces rather than undercuts the `cMap` scarcity — room/user/chat lookups in this codebase are O(n) list scans via `cQ`, and `cMap` (real `std::map`) is reserved for the two cases that plausibly need faster keyed lookup (server registry, channel registry).

### 9. What actually crashed this session, traced end to end

A real captured run (`/tmp/emu.log` + `~/.emu32/dblog.txt` + `/tmp/emu_crash.json`, all from the same run, timestamp-consistent) gives a complete, non-speculative chain — worth walking in full since it overturns §1's original theory.

**The crash itself**: `95.869s [ERROR] [exception] heap allocator ran into THREAD_STACK_BASE: alloc of 2040 bytes at 0x7fff930 would push the heap cursor to 0x8000130, past THREAD_STACK_BASE (0x8000000)`. The faulting EIP (`0x00200142`) sits two bytes inside the `kernel32.dll!HeapAlloc` trampoline (`0x200140`) — this is a real Win32 `HeapAlloc(2040 bytes)` call, on `tid=1011` (the DB thread — matches `--DBThread is alive! (han=0xBEF9 threadid=0x3F3)`, `0x3F3` = 1011 decimal). Cursor position `0x7fff930` means ~63.999MB of the 64MB region was *already* consumed before this ordinary, modest request finally couldn't fit. This is cumulative exhaustion, not one big request.

**Tracing what filled the heap**: `dblog.txt` shows the DB thread going online (`Dbcode_StartSingleRaceAndOnlineMode`), then `Dbcode_GetStockCarList` (`0x008f12b0`, decompiled) running:
1. `DBMem_Alloc(0x18)` = 24 bytes — a `DBStockListOutputData` header (matches `dbcode.c(1976)` in the log).
2. `DBMem_Alloc(225 * 1604)` = 360,900 bytes — **one** preallocated array sized for up to 225 cars, 1604-byte stride each (matches `dbcode.c(1993)` and the log's own `car count=154 (1604)` line exactly). Only 154 of the 225 slots get used — this is a single, bounded, one-shot buffer, not a growth driver by itself.
3. A fetch loop fills in the 154 car records into that one buffer (no further allocation).
4. **Two per-car loops follow**, each iterating all 154 cars: the first calls `DBParts_BPT2CarEra(bptid)` then `DBParts_GetStockCarPerformance(bptid)` for every car (308 calls total); the second calls `DBParts_GetBrandedPartDefInfo(...)` for every car (154 calls, not yet decompiled).

**Both `DBParts_BPT2CarEra`** (`0x0095c260`, decompiled) **and `DBParts_GetStockCarPerformance`** (`0x0095b780`, decompiled) **are individually clean**: both are thin wrappers that construct a stack-local `DBParamQuery`/`DBRecordset` pair, run a query, and destruct both objects on *every* code path (success, mismatch, failure) — no `operator new`, no heap allocation of their own. And both run the **exact same query string**, `"StockVehicleAttributes_SelectClass"` — meaning this one step alone executes that query 308 times (154 cars × 2 identical lookups).

**Where that leaves it**: since neither wrapper function leaks at the C++ level, and dblog.txt shows *zero* `DBMem_Free`/`DBMem_Realloc` calls across the entire run (nothing tracked through that path is ever released), whatever accumulates has to be inside the **real DAO/COM object lifecycle** that `DBParamQuery`/`DBRecordset`'s own constructors/destructors and `DoQuery`/`Fetch` wrap — i.e., tew's own emulation of that COM machinery, not the game's frontend code. This lines up with a gap already known from pure code review earlier this session: `ole32_handlers.py`'s `_CoCreateInstanceEx` (gap #10 in the table below) leaves scratch COM pointers unfreed. If tew's COM/DAO object release path is incomplete more broadly, 308+ repeated query executions (just from this one function) would leak real, modest per-call bytes that never show up as `DBMem_Alloc` traffic and never get reclaimed — a slow, silent climb from routine gameplay-adjacent DB activity, not from any single big allocation. This is a very different shape of problem than §1's `_MEM_init` theory: it's death by a thousand small COM leaks, not one big undersized arena.

### 10. Confirmed directly: wired up a real `_CrtDumpMemoryLeaks` call on crash, ran it

Rather than keep inferring from what's ruled out, tew's own crash path (`tew/kernel/exception_diagnostics.py`) now calls the guest's real `_CrtDumpMemoryLeaks` (`0x009F81B0`) via a nested `_invoke_emulated_proc` call right before finalizing an unhandled fault — the same re-entrant-call mechanism already used for `__pfnReportHook` forwarding. Two real obstacles came up and were fixed along the way:

- **The game's own registered `_CRT_REPORT_HOOK`** (`crtReportHookCallback`, `0x006881A0` — the exact function already named in `patch_internals.py`'s comments as what drives `memleaksCRT.txt`) ends in a bare `INT 3` whenever it isn't actively mid a single leak-report burst. Real MSVC debug-CRT-hook behavior, not a guest bug, but not something this diagnostic call has any business triggering — fixed by temporarily zeroing `_CRT_REPORT_HOOK_PTR` (`0x020ee23c`) for the duration of the dump, since tew's own unconditional log line inside `_crt_dbg_report` doesn't need the hook to fire at all.
- **`patch_internals.py`'s `_crt_dbg_report` only ever substituted a single `%s` or `%d`** in report format strings — real leak-dump lines use `%hs`/`%08X`/`%u`/`%ld`, none of which matched, so every line printed its format string literally with no data. Fixed by routing substitution through the already-existing, already-correct shared printf engine (`_sprintf_format` in `msvcrt_handlers.py`, the same one `sprintf`/`printf` use) instead of the old ad hoc check.

**Real result from a live run**: a complete leak dump, "Object dump complete." reached cleanly. **10,943 leaked blocks, 45,578,803 bytes (43.47MB) of the 64MB heap** — this is now a measured fact, not an inference.

Splitting it by size tells two different stories:
- **One single 42MB block** (`44,040,192` bytes / `0x02A00000`, allocation `#522`) — no file/line attribution at all (went through a plain `malloc()`, not the debug-instrumented `operator new`). **Now attributed for real — see §11.** Not `_MEM_init`'s arena (that's a real, separate 64MB request, confirmed still not it by exact size); it's a *sibling* call, `Platform_SysStartUp` (`0x006b162b`) → `_MEM_initsize(0x2a00000)`, setting up `_Platform_gSysInfo`. This is **96% of all leaked bytes in one allocation** — confirmed legitimate, still-alive, by-design: it's an engine arena that shows up as "leaked" only because the process never reaches a clean exit, feeding straight into `Parts_InitMem()`/`CarLoad_InitMem()` right after.
- **`dbcode.c(4024)`: 10,426 separate 16-byte allocations, never freed** — 95%+ of the leaked block *count*, though only ~167KB total. This is the real, unambiguous bug signal: the same tiny allocation from the same exact source line, repeated over ten thousand times with no matching free anywhere in the run. Not yet decompiled — `dbcode.c` line 4024 is well past the `Dbcode_GetStockCarList`/`DBParts_*` functions already examined (which sit around lines 1976-2025), so this is a different function, most plausibly connected to the `DBServiceRequestQ` per-request ticket bookkeeping (dblog.txt showed numbered requests like `#704 DBT_GO_SINGLERACE`) given it sits alongside `DBQuery.c` hits in the same dump.
- Smaller, also real: `DBQuery.c(557)`/`DBQuery.c(758)`/`DBQuery.c(1002)` (~138KB combined, tens of blocks each) and `DBApt.c(600)`/`DBApt.c(1003)` (~510KB combined, only 2 blocks each but ~150KB apiece). `DBQuery.c` is almost certainly the file implementing `DBParamQuery`/`DBRecordset` — the exact classes flagged as the leak candidate in §9, now with real per-line confirmation instead of elimination-by-inference.

**Files changed** (branch `crt-leak-report-on-crash`): `tew/kernel/exception_diagnostics.py` (new `_dump_crt_memory_leaks` helper, wired into `diagnose_fault`), `run_exe.py` (passes `memory`/`state` into `diagnose_fault`), `tew/api/patch_internals.py` (`_crt_dbg_report` now uses `_sprintf_format`), `tests/unit/api/test_patch_internals.py` (3 tests updated to match the corrected substitution behavior, 1 new regression test added). All 1183 tests pass.

**Not yet done**: decompiling `dbcode.c(4024)`'s actual call site to confirm what it allocates and why nothing ever frees it (the clearest, most actionable next step).

### 11. Confirmed directly: allocation `#522` (the 42MB block) is `Platform_SysStartUp`'s `_MEM_initsize(0x2a00000)`, not `_MEM_init`'s arena — plus found the game's own OOM diagnostic path

§10 left the 42MB block's identity as "not confirmed which." Resolved for real this session, ground-truth verified rather than inferred from decompile alone:

**The instrumentation**: MSVC's debug CRT keeps its own allocation-request counter as a plain `long` global at `DAT_01280804` — confirmed via decompile of `__heap_alloc_dbg` (`0x009f6460`): `iVar4 = DAT_01280804` (read, pre-increment) gets stored as the new block header's `lRequest` field (`puVar5[6]`) a few lines later, right before `DAT_01280804 = DAT_01280804 + 1`. The read happens at `0x009f64ac` (`MOV EDX, dword ptr [DAT_01280804]`). Added a `cpu.add_logpoint(0x009f64ac, ...)` probe (originally on the throwaway `crt-request-counter-instrumentation` worktree, ported here) that reads `DAT_01280804` directly (gives the exact pre-increment request number) alongside the requested size at `[EBP+8]` (`param_1`, confirmed via the function's standard `PUSH EBP; MOV EBP,ESP; SUB ESP,N; PUSH EBX; PUSH ESI; PUSH EDI` prologue). Fired 11,019 times cleanly across a full ~90s run to the same known heap-ceiling fault (2040 bytes past `THREAD_STACK_BASE`) documented in §9/x45 — that run predates this branch's real `free()`/reclaim (see status.md), so the ceiling was still hit; the instrumentation and its result stand independent of that fix.

**Direct hit**: `request=#522 size=44040192` at 6.410s into the run, on the main thread (`tid=1000`) — exact match, both fields. Also confirmed the single largest allocation across the *entire* run (next-largest: `360,900` bytes, the `DBMem_Alloc` car-array buffer from §9) — reinforcing it really is a one-shot engine arena, not a repeated pattern.

**Real call site, found via `0x02A00000` (the hex form of `44,040,192`) as a search anchor in the decompile**: `Platform_SysStartUp` (`0x006b162b`):
```c
_Platform_gSysInfo = _MEM_initsize(0x2a00000);
if (_Platform_gSysInfo < 0x29ce000) {
    abortmessage("Out of memory! %d", _Platform_gSysInfo);
}
Parts_InitMem();
CarLoad_InitMem();
```
`_MEM_initsize` (`0x00a71990`) does the actual `_malloc(param_1)` itself — plain, untracked, matching the leak dump's "no file/line" description exactly — then hands the resulting pointer to `_MEM_initadr` (`0x00a718f0`) to build the pool structure around it (`_MEMCLASS_create(0, "RAM", ptr, size, ...)`). `_MEM_initadr` doesn't allocate anything itself; it's the "turn this raw block into a slicing-capable arena" step, called *after* the real allocation already happened. Feeding straight into `Parts_InitMem()`/`CarLoad_InitMem()` right after strongly suggests this is the arena car-parts/object data gets loaded into.

**A second, real, separate finding along the way — the game's own OOM diagnostic path**: `_REAL_init` (`0x00a34980`) sets `_printmemptr = _MEM_print;` (a `DATA` reference, not a call — confirmed by creating the previously-undefined function boundary at `0x00a71dd0` and checking its real xrefs). `_printmemptr` is read by both `abortmessage` variants (the same ones seen throughout this doc, e.g. `Platform_SysStartUp`'s own "Out of memory!" call above) — meaning `_MEM_print` → `_MEM_printclass` (`0x00a71db0`) → `_MEM_printclassf` (`0x00a71b40`) is the game's *own* diagnostic dump, fired at the moment one of its internal `_MEM_*` arenas actually runs out. `_MEM_printclassf` walks a memory-class's block list (same `_memclass[idx]` structure `_MEM_initadr` builds) printing each block's name/address/size/type/CRC/sentinel-validity, ending with `"TOTAL FREE MEMORY: %ld ($%08lx)"` for that class — a real, per-block, per-class usage report the game already knows how to produce, distinct from tew's own `_CrtDumpMemoryLeaks` path and potentially a much more direct signal for the `_MEM_*`-arena side of any future heap-sizing decision. A cluster of other `_MEM_*`-prefixed static globals sits in the same data region as `_printmemptr` (`_memclass`, `_MEMlowadr`, `DAT_020f3364` seen already) — plausibly the memory manager's full bookkeeping state, consistent with linker grouping of statics from the same source files (`cmn/meminit.c`, `cmn/memprint.c`, both named in real abort-message strings already seen in this doc).

**Not yet done**: tracing `_printmemptr`'s own call sites (not just its two `abortmessage`-path reads) to confirm exactly when it fires during normal execution; decompiling `dbcode.c(4024)` (still the clearest actionable next step, unchanged from §10); **re-running the full leak-dump investigation now that real `free()`/reclaim exists on this branch** — §9-§11's numbers were all measured against a bump allocator that never reused an address, so every "leaked" block genuinely was still live; with reclaim in place, a rerun should be treated as a fresh measurement, not an update to these figures.

## Open threads / not yet looked at

- **`cSendMsgQ`/`cReadMsgQ`** (`0x00ac9c10`/`0x00acbda0`) — haven't decompiled these yet, only found them as `cQ` callers. Next concrete step: decompile both to see what they actually queue (raw `NPSMessageContainer*`? something else?) and where the enqueue/dequeue call sites live relative to `DeliverMessageFunc`/`NPSThreadSender`.
- Live numbers for `MessagePool`'s `msgSize`/`maxCount` — would need to read the actual `SocketInitializer` config values (either from source config or from a live run's memory) rather than just the decompiled call shape.
- ~~How many `TCPMgr`/`CommMgr`-style instances actually exist concurrently~~ — resolved: it's the same two threads already known from the `gNPS` investigation (`0xBEF0`/`0xBEF1`), not a separate additional thread pool. `TCPMgr` itself is confirmed active client-side, not server-only — per §2, it's the MCOS/Transactions/DB-thread transport, exercised even in offline "SingleRace" mode via the numbered `DBServiceRequestQ` tickets.
- ~~Whether `DBMem_Alloc`'s `node:`/`buf:` pairs are `MessageNode` instances~~ — resolved via decompile, they're not. `DBMem_Alloc` (`0x00926310`) is `struct BufferNode * __cdecl DBMem_Alloc(long)` (Ghidra's own recovered signature). Its real work happens in `MemoryPool::Get(this, size)` (`0x0040371a`): past the pool's small-block threshold, it does `node = operator_new(0xc)` — a 12-byte `BufferNode` (`buffer` ptr, `size`, an `is_large` flag) — then a **separate** `operator_new(size)` for the actual payload, stored at `node->buffer`. That's exactly the `node:`/`buf:` pair in the log — two distinct allocations, wrapper + payload. Below the threshold it recurses into a no-arg `Get(this)` that pulls a recycled node from the pool's internal small-block free list (the `0x800`/`0x28` setup from `DBMem_Init`). `BufferNode` is DBMem's own private type — architecturally parallel to `MessageNode`/`MessagePool` (small header + payload, pool-recycled when possible) but a completely separate struct from the networking layer's `MessageNode`. There's already a distinct `KList<BufferNode*, BufferNode*>` (`0x00928410`) confirming this as its own tracked type.
- Where `NPSMessageContainer`/`NPSMessageContainerGC` objects actually get freed (GC subclass name suggests some kind of ref-counted or deferred cleanup — worth understanding before assuming these are short-lived).
- Whether any of the other `cQ`-based lists (`cNPS_UserList`, `cContactList`, `cNPSC_Persona`, `cNPSC_Mail`, etc.) get pre-sized/reserved anywhere at a higher level even though `cQ` itself doesn't — worth a quick check before assuming all of them are pure incremental growth.

## tew's 10 confirmed gap sites (kept for reference, not urgent per the findings above)

| # | Site | Gap |
|---|---|---|
| 1 | `patch_internals.py:190,192` `_crt_dbg_report` | `msg_ptr`/`retval_ptr` scratch buffers never freed |
| 2 | `dsound_handlers.py` `_ds_create_sound_buffer`/`_ds_duplicate_sound_buffer` (226, 281) + `Buf::Release` (513-515) | `Buf::Release` is a hardcoded no-op stub, never frees `pcm_addr` |
| 3 | `kernel32_handlers.py:296` `_authlogin_alloc` | No corresponding free redirect patched |
| 4 | `kernel32_io.py:888-916` `_unmap_view_of_file` | Never pops `heap_alloc_sizes` |
| 5 | `kernel32_io.py:2162` `_format_message_a` (ALLOCATE_BUFFER path) | Allocates outside `_local_alloc`'s tracking |
| 6 | `kernel32_io.py:2567,2575-2579` `_local_alloc`/`_local_free` | `_local_free` doesn't pop `heap_alloc_sizes` |
| 7 | `kernel32_io.py:2585,2592-2594` `_global_alloc`/`_global_free` | `_global_free` is a complete no-op |
| 8 | `msvcrt_handlers.py:505-519` `_realloc` | Doesn't free old ptr |
| 9 | `msvcrt_handlers.py:525-529` `free`, `540-544` `operator delete`, `patch_internals.py:337-340` `__free_dbg` | All three are no-ops |
| 10 | `ole32_handlers.py:478,482` `_CoCreateInstanceEx` | `factory_ppv`/`unk_ppv` scratch pointers never freed |

## Where this leaves the heap-sizing question

Nothing here is a decision yet — just the shape of the problem as currently understood, now split into two genuinely different failure modes rather than one:

- **A real but so-far-unobserved sizing risk**: the mandatory 64MB `_MEM_*` arena, invisible to tew once granted, exactly matches tew's entire current heap region. This hasn't been caught in the wild yet, but remains a real one-shot risk worth accounting for eventually.
- **The actual observed failure mode (§9), which looks completely different**: slow cumulative growth from real, repeated DAO/COM query traffic (308 identical queries just from one car-list-loading step) whose underlying COM object lifecycle likely isn't releasing everything it should — plausibly tew's own known `_CoCreateInstanceEx` gap or a similar incomplete COM release path, not a game-side leak. This is a reclamation problem after all, just not the one originally guessed at, and not fixable by the free-list-for-tew's-10-known-gap-sites plan either, since none of those 10 sites are in the DAO/COM path.
- Other real, smaller, visible contributors: `DBMem_*` (variable, real sizes seen: 24B–360KB per call, no frees observed), `MessagePool` × 3 per `TCPMgr` (bounded, config-driven, not yet read live), the `cQ`-backed object lists (unbounded, purely incremental), `cMap`'s two real uses (ordinary `std::map`, no batch allocation), normal CRT/Win32/COM/d3d8 traffic, and tew's own 10 known small leaks (real, but now confirmed *not* what caused this session's actual crash).
- The d3d8 double-allocator collision (a separate, tew-side bug, unrelated to any of the above).

Given §9, the highest-value next step if/when we pick this back up is probably confirming whether tew's own COM/DAO release handling is the real culprit — not sizing the heap bigger (which would just delay the same slow leak, not fix it) and not the original free-list plan (which never touched this path at all).
