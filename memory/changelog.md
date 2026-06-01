# Emulator Changelog (Python port)

Entries are newest-first.

---

## 2026-05-31 — Implement CharUpperA

**`tew/api/user32_handlers.py`** — added `_CharUpperA` handler.
Both spec branches: HIWORD==0 → single-char uppercase return; HIWORD!=0 → string
pointer, uppercase ASCII ('a'-'z') in-place, return pointer. Non-ASCII unchanged.

**Result:** Game proceeds past step 133,014,085 (4,546 steps past CharUpperA).
New blocker: `ifc22.dll!??0CImmMouse@@QAE@XZ` (CImmMouse constructor).

---

## 2026-05-31 — Implement GetFullPathNameA

**`tew/api/kernel32_io.py`** — replaced `_halt("GetFullPathNameA")` with real handler.
Resolves relative paths against `C:\MCity`; normalises `..` and `.` segments; writes
to lpBuffer and sets *lpFilePart. Returns char count on success, required size if buffer
too small.

**Result:** Game proceeds past step 133,014,079. New blocker: `user32.dll!CharUpperA`.

---

## 2026-05-31 — Implement Dev::DrawPrimitive (Vulkan geometry rendering)

**New file:** `tew/api/d3d8/_pipeline.py`
- SPIR-V passthrough vertex/fragment shaders encoded as Python word lists (no external compiler)
- `create_image_views`: VkImageView per swapchain image
- `create_render_pass`: single color attachment, loadOp=LOAD (preserves cleared background)
- `create_framebuffers`: one VkFramebuffer per swapchain image
- `create_pipeline`: graphics pipeline — XYZRHW+DIFFUSE vertex layout (stride=32), dynamic viewport/scissor, no culling
- `create_vertex_buffer`: 4MB host-visible/host-coherent Vulkan buffer, permanently mapped
- `init_pipeline`: orchestrates all of the above

**`tew/api/d3d8/_state.py`** additions:
- `_vk_image_views`, `_vk_render_pass`, `_vk_framebuffers`, `_vk_pipeline`, `_vk_pipeline_layout`, `_vk_vertex_buffer`, `_vk_vertex_memory`, `_vk_vertex_mapped_ptr`
- `_vk_in_render_pass` flag
- `_draw_stream_ptr`, `_draw_stream_stride`, `_draw_vertex_fvf`

**`tew/api/d3d8/idirect3d8.py`** — CreateDevice calls `init_pipeline` after swapchain + sync primitives are ready.

**`tew/api/d3d8/idirect3d8device.py`** changes:
- `SetStreamSource`: now stores vertex buffer data pointer and stride in `_state`
- `SetVertexShader`: stores FVF handle in `_state._draw_vertex_fvf`
- `BeginScene`: adds `TRANSFER_DST → COLOR_ATTACHMENT` barrier + `vkCmdBeginRenderPass`
- `EndScene`: calls `vkCmdEndRenderPass` (render pass finalLayout handles `PRESENT_SRC_KHR` transition)
- `Clear`: uses `vkCmdClearAttachments` when inside render pass, `vkCmdClearColorImage` otherwise
- `DrawPrimitive(slot 70)`: transforms XYZRHW→NDC in Python, uploads to Vulkan buffer, issues `vkCmdDraw`

**Result:** Game now renders geometry. DrawPrimitive fires (prim_count=1 triangles).
New blocker: `GetFullPathNameA` (kernel32, file I/O) — game has advanced past rendering.

---

## 2026-05-31 — Fix D3DRES_VTABLE buffer slot ordering

**Root cause diagnosed:** `D3DRES_VTABLE` (used by vertex/index buffer objects) had Surface methods
at slots 11–14. dx8z calls Lock (slot 11) and Unlock (slot 12) on vertex buffers per the D3D8 spec,
but our vtable had Surface::GetContainer (arg_bytes=8) and Surface::GetDesc (arg_bytes=4) there.
Each Lock call (which pushes 4 args = 16 bytes) hit GetContainer's 8-byte cleanup, leaving 8 bytes
uncleaned. After 100K steps this corrupted EBP → 0x020d9228 (.data) and the LEAVE+RET sequence
popped a .data value (0x2196) as the return address → RUNAWAY.

**Files changed:**
- `tew/api/d3d8/idirect3d8resource.py`: Reduced from 18 to 14 slots. Removed dead Surface
  methods (slots 11–14 — surfaces use D3DSURF_VTABLE, not D3DRES_VTABLE). Buffer methods now at
  correct spec positions: Lock(11, arg_bytes=16), Unlock(12, arg_bytes=0), GetDesc(13, arg_bytes=4).
  Also fixed `_get_device` which read ESP+4 (= `this`) instead of ESP+8 (`ppDevice`).
  Implemented `_buffer_get_desc` to fill D3DVERTEXBUFFER_DESC with size from object field.
- `tew/api/d3d8/_layout.py`: Updated D3DRES_VTABLE comment (14 × 4 = 56 bytes).

**Result:** RUNAWAY at 132.7M eliminated. Game now halts loudly at `Dev::DrawPrimitive`.

---

## 2026-05-31 — DirectInput stub + cursor/keyboard handlers

**New file:** `tew/api/dinput_handlers.py`
- `DirectInputCreateA` (dinput.dll + dinput8.dll) — returns DI_OK, writes singleton IDirectInput2 COM object to `*lplpDirectInput`
- IDirectInput2A vtable (9 slots @ DI_VTABLE=0x00220290, singleton @ DI_OBJ=0x002202C0): QI, AddRef, Release, CreateDevice (heap-allocates device obj), EnumDevices (S_OK, no callbacks), GetDeviceStatus (DI_NOTATTACHED), RunControlPanel (E_NOTIMPL), Initialize (S_OK), FindDevice (DIERR_DEVICENOTREG)
- IDirectInputDevice2A vtable (18 slots @ DI_DEV_VTABLE=0x002202D0): full set including GetDeviceState (zeroes output buffer), GetDeviceData (pdwInOut=0), SetDataFormat/SetCooperativeLevel/Acquire/Unacquire (all S_OK)

**user32_handlers.py additions:**
- `GetCursorPos` → writes (0,0), returns TRUE
- `ScreenToClient`, `ClientToScreen` → no-op, returns TRUE (window at screen origin)
- `SetCursorPos` → no-op, TRUE
- `SetCapture` → 0, `ReleaseCapture` → TRUE, `GetCapture` → 0
- `ClipCursor` → TRUE, `MapWindowPoints` → 0
- `GetAsyncKeyState` → 0 (key up)
- `GetKeyboardState` → zero-fills 256-byte table, TRUE
- `GetKeyboardType` → 4/0/12 for type/subtype/fkeys (Enhanced 101-key)
- `MapVirtualKeyA` → 0

**Result:** Game now reaches the render loop. RUNAWAY at step 132.7M (new blocker).

---

## 2026-05-31 — IDirect3DSurface8 vtable fix + BeginScene deadlock fix

**Root cause 1:** D3DRES_VTABLE had IDirect3DResource8 slot ordering (slot 9 = PreLoad),
but dx8z expected IDirect3DSurface8 ordering (slot 9 = LockRect). `_THRASH_lockwindow`
crashed on INT 0xcd because it called LockRect through the wrong vtable.

**Fix:** New `D3DSURF_VTABLE` @ 0x00220260 with correct 11-slot IDirect3DSurface8 layout.
`_alloc_surface_obj(w, h, fmt, memory)` allocates 24-byte COM objects with vtable,
data ptr, size, width, height, and D3DFORMAT. GetDesc reads these fields; LockRect
fills D3DLOCKED_RECT with correct pitch (w×4) and data pointer.
All surface-returning device methods (`GetBackBuffer`, `CreateTexture`,
`CreateRenderTarget`, `CreateDepthStencilSurface`, `CreateImageSurface`,
`GetRenderTarget`, `GetDepthStencilSurface`, `CreateVolumeTexture`, `CreateCubeTexture`)
switched to `_alloc_surface_obj`.

**Root cause 2:** BeginScene deadlock on frame 2+. `vkWaitForFences` was called directly
on the main thread; Mesa/Wayland WSI can call `wl_display_roundtrip` internally, which
needs the main thread to pump events — deadlock.

Also: dx8z calls BeginScene/EndScene twice during THRASH init without any Present
between them. The second BeginScene called `vkWaitForFences` on an unsignaled fence
(reset in frame 1, never re-signaled because QueueSubmit was never called).

**Fix:** Moved `vkWaitForFences` + `vkResetFences` into `vk_pump` background thread.
Added `_vk_frame_submitted` flag (set by Present after QueueSubmit, cleared by
BeginScene) to gate the wait. Added `_vk_image_acquired` flag to skip re-acquiring
when BeginScene is called multiple times without Present.

**Result:** Game now reaches `dinput.dll!DirectInputCreateA` (new blocker).

---

## 2026-04-25 — Beta binary CD check investigation

Investigated why mcity_beta_1.exe shows "Game CD not found" despite instLev=2 in registry.

**Root cause:** The beta binary's CD check function (~0x4d27c0) reads instLev and sets
bMaxInstall at 0x975f78, but has no conditional skip of the CD loop — it unconditionally
calls SetErrorMode(0x8001), loops GetDriveTypeA over A:–Z:, then returns 2 (failure) if no
game CDROM found. The debug binary has Platform_IsMaxInstall (0x52dc30) which reads from
a different global (0xa10f50) and skips the loop; the beta binary lacks this path entirely.

**Key addresses (beta binary):**
- CD check function entry: ~0x4d27c0 (complex SEH prologue, thiscall)
- instLev check + bMaxInstall set: 0x4d2a11–0x4d2a31
- SetErrorMode call (start of CD loop): 0x4d2a7b, return to 0x4d2a81
- Inner drive check (calls GetDriveTypeA): 0x4d56b0
- Loop back: 0x4d2ae0 (JL 0x4d2a86, 26 drives)
- SetErrorMode restore + CD-found check: 0x4d2ae6, CMP ESI,EDI at 0x4d2aec
- Failure path (return 2): 0x4d2afc; success path (return 0): 0x4d2bb8

**Planned fix (deferred):** return DRIVE_CDROM for the install drive in GetDriveTypeA
handler — binary-agnostic, no address-specific patches needed.

**Diagnostics removed:** SetErrorMode return-address logging, Platform_IsMaxInstall patch.
GetDriveTypeA first-call trace kept.

---

## 2026-04-25 — TDD sweep: fix silent failures, implement missing handlers, port tests to ZigCPU

**Silent failure fixes:**
- `msvcrt._realloc`: was returning a new pointer but discarding old data; now copies
  `min(old_size, new_size)` bytes using `heap_alloc_sizes` to find old allocation size.
- `msvcrt._write`: was ignoring the fd entirely; now routes to host fd via `os.write`,
  with fallback for raw stdout/stderr (fd 1/2) when no file handle entry exists.

**Missing handler implementations:**
- `advapi32.RegEnumKeyExA` / `RegEnumValueA`: fully implemented direct-child enumeration
  from the flat `registry_values` dict using backslash-prefix filtering.
- `kernel32.TryEnterCriticalSection`: three-case implementation — recursive by owner
  (TRUE), free CS (acquire + TRUE), contested by other thread (FALSE, no blocking).
- `user32.CallNextHookEx`: full LIFO chain propagation via `_winhook_chains`; each
  handle knows its position in the chain and invokes the next via `_invoke_emulated_proc`.
- `user32._dispatch_winhooks`: fixed to call only the chain head (not all hooks).
- `oleaut32`: added `logger.warn` to previously silent stubs (VarCyFromStr, LoadTypeLibEx,
  RegisterTypeLib, CoCreateInstance).

**Test suite (543 passing):**
- New API unit tests: msvcrt realloc + write, registry enum happy + invalid paths,
  CS (Init/Enter/Leave/TryEnter) and mutex (Create/Wait/Release/Close) paths,
  hook chain happy + sad paths.
- Hypothesis chaos tests: `@given` invariants for CS/mutex, garbage-memory fuzzing,
  `RuleBasedStateMachine` for arbitrary Enter/TryEnter/Leave sequences.
- Opcode tests ported from Python CPU to ZigCPU black-box execution (all 5 files).
- Deleted `test_cpu.py` — tested Python CPU internals, not the production ZigCPU path.

**Run result:** With server running, game logs in, initializes D3D8, and hangs at
`IDirect3D8::CreateDevice` (Wayland deadlock — unchanged blocker).

---

## 2026-04-24 — GetMessageA cooperative yield + Wayland roundtrip attempt

**`GetMessageA` cooperative yield (`user32_handlers.py`):**
- Fixed broken cooperative-yield path. Old code set `cpu.halted = True` +
  `state.thread_yield_requested = True`; `thread_yield_requested` was never read
  anywhere, so it terminated the run loop instead of yielding.
- New code: `state.scheduler.sleep_current(cpu, memory, retry_eip, 0, 1)` — saves
  thread state at stub entry EIP, marks thread SLEEPING, switches to next READY thread.

**`IDirect3D8::CreateDevice` — Wayland roundtrip (`d3d8/idirect3d8.py`):**
- Added `wl_display_roundtrip(wl_display)` via ctypes after `vkCreateWaylandSurfaceKHR`,
  before any surface-property queries (`vkGetPhysicalDeviceSurfaceSupportKHR` etc.).
- Rationale: Wayland compositor needs to process events before it can respond to
  surface capability queries; without this, those calls deadlock.
- Status: hang persists — exact call still unknown. Next: fine-grained step logging
  inside the surface creation block to isolate which call deadlocks.

**Tests:** 450 (all passing).

---

## 2026-04-24 — BeginPaint/EndPaint + full Vulkan swapchain

**`BeginPaint` / `EndPaint` (`user32_handlers.py`):**
- Implemented `BeginPaint(HWND, LPPAINTSTRUCT) → HDC` using the existing
  `_alloc_hdc` infrastructure. Fills the 64-byte PAINTSTRUCT: hdc, fErase=0,
  rcPaint from window entry cx/cy, all reserved bytes zero.
- Implemented `EndPaint(HWND, LPPAINTSTRUCT)`: reads hdc from PAINTSTRUCT,
  removes it from `_live_hdcs` / `_dc_selected`, returns TRUE.
- Both registered as user32.dll stubs with stdcall 8-byte cleanup (2 args).

**`IDirect3D8::CreateDevice` (`d3d8/idirect3d8.py`):**
- Now builds the complete Vulkan rendering backend:
  - Creates `VkSurfaceKHR` from the SDL window's SDL_GetWindowWMInfo X11/Wayland
    display and window handles via `vkCreateXlibSurfaceKHR` /
    `vkCreateWaylandSurfaceKHR` (selected by WAYLAND_DISPLAY).
  - Destroys the SDL renderer on the game window (Vulkan takes over).
  - Finds a queue family with graphics + present support.
  - Creates `VkDevice` with `VK_KHR_swapchain`.
  - Creates `VkSwapchainKHR` (format B8G8R8A8_UNORM preferred, FIFO present mode,
    extent from D3DPRESENT_PARAMETERS or surface capabilities).
  - Allocates command pool + single `VkCommandBuffer`.
  - Creates 2 semaphores (`image_available`, `render_done`) + 1 fence
    (`in_flight`, pre-signalled).
  - All failures halt with explicit error log.
- `make_vtable` now accepts `window_manager` parameter; threaded through from
  `register_d3d8_handlers(stubs, memory, state)` in crt_handlers.py.

**`BeginScene` / `EndScene` / `Clear` / `Present` (`d3d8/idirect3d8device.py`):**
- `BeginScene`: waits for in-flight fence, acquires swapchain image, records
  UNDEFINED → TRANSFER_DST_OPTIMAL barrier, begins command buffer.
- `Clear`: records `vkCmdClearColorImage` (D3DCOLOR ARGB → float RGBA; only when
  D3DCLEAR_TARGET flag set).
- `EndScene`: records TRANSFER_DST_OPTIMAL → PRESENT_SRC_KHR barrier, ends
  command buffer.
- `Present`: `vkQueueSubmit` with image_available wait + render_done signal +
  in_flight fence; `vkQueuePresentKHR`.
- All four were previously `_halt`; now real Vulkan.

**State (`d3d8/_state.py`):**
- Added: `_vk_device`, `_vk_*_queue_family`, `_vk_*_queue`, `_vk_surface`,
  `_vk_swapchain`, `_vk_swapchain_*`, `_vk_command_pool`, `_vk_cmd_buf`,
  `_vk_image_available`, `_vk_render_done`, `_vk_in_flight`,
  `_vk_current_image_idx`, `_vk_fn_*` extension function slots.

**Tests:** 450 (all passing).

---

## 2026-04-24 — Round-robin preemption

**Round-robin preemption (`scheduler.py`, `run_exe.py`):**
- Added `Scheduler.preempt_slice(cpu, memory)`: after each `cpu.run(batch)`, if the
  current thread is READY and another READY thread exists, rotate to it.
- Called in the main run loop immediately after `cpu.run(batch)`.
- Root cause: `mmtimer_callback` (0x00a30a40) signals its own wait event (0x7012) via
  `_SIGNAL_set` inside the `_tmrsub[]` dispatch loop, so `WaitForMultipleObjectsEx`
  always found the event signaled and never yielded. The timer thread consumed 100% of
  emulated CPU, starving all other threads.
- Result: at 132M steps the main game window (`Motor City Online` HWND 0x1034) is
  created and the game progresses further than before.

**Tests:** 450 (up from 388).

---

## 2026-04-23 — CreateDialogParamA fix, timer dispatch, advapi32 time source

**`proc=0` / 122-second stall (`user32_handlers.py`):**
- `CreateDialogParamA(#106)` fixed with null-guard.

**`PendingTimer.fu_event` — `timeSetEvent` dispatch modes (`kernel32_io.py`):**
- `TIME_CALLBACK_FUNCTION` (0x00): invoke emulated proc
- `TIME_CALLBACK_EVENT_SET` (0x10): SetEvent on handle directly

**advapi32 `timeSetEvent` time source (`advapi32_handlers.py`):**
- Fixed to use `state.virtual_ticks_ms + u_delay`.

---

## 2026-04-21 — Cooperative CS blocking, mutex owner tracking

**Cooperative CriticalSection blocking (`kernel32_sync.py`, `kernel32_system.py`, `kernel32_io.py`, `_state.py`):**
- `_enter_cs`: contested background thread now suspends cleanly — undoes LockCount
  increment, sets `thread.waiting_on_cs = ptr`, sets `thread_yield_requested = True`,
  halts without `cleanup_stdcall` so EIP stays at stub for retry on resume.
  `_leave_cs`: full release resets LockCount = -1 and OwningThread = 0 (replaces old
  decrement + halt-if-waiters). Removed noisy per-release debug log.
- Scheduler (`_cooperative_sleep` + `_run_background_slice`): added `waiting_on_cs`
  check — if `OwningThread == 0`, clears `waiting_on_cs` and allows thread to retry.
- `PendingThreadInfo`: added `waiting_on_cs: Optional[int] = None`.
- Result: NPS networking threads (tid=0x3ec, 0x3ed) survive CS contention; game
  reaches and sustains the rendering loop (BeginScene/SetRenderState cycling) at 129M+ steps.

**Mutex owner tracking (`kernel32_io.py`, `_state.py`):**
- `MutexHandle`: added `owner_tid: Optional[int]` and `recursion_count: int`.
- `WaitForSingleObject`: mutex now checks `owner_tid is None` before acquiring.
  Contested mutex blocks via `waiting_on_handles` (same path as unsignaled event).
  Recursive acquisition by owning thread increments `recursion_count` and returns 0.
- `WaitForMultipleObjectsEx`: mutex ready only when `owner_tid is None`.
- `ReleaseMutex`: decrements `recursion_count`; clears `owner_tid`/`locked` on zero.
- `CreateMutexA`: sets `owner_tid` + `recursion_count = 1` when `bInitialOwner != 0`.
- Scheduler checks updated to use `owner_tid is None` instead of `not obj.locked`.

**Tests:** 388 (unchanged).

---

## 2026-04-20 — TEB/PEB truthfulness, CriticalSection fix, kernel32 split

**TEB/PEB truthfulness (`kernel32_system.py`, `kernel32_sync.py`, `kernel32_io.py`,
`kernel_structures.py`, `_state.py`):**
- `SetLastError`/`GetLastError` now read/write TEB memory at `TEB_BASE + 0x34`.
  Previously used Python `state.last_error` field — binary code doing `MOV EAX, FS:[0x34]`
  directly would get stale zero. `state.last_error` removed entirely.
- `TlsSetValue`/`TlsGetValue` now read/write TEB memory at `TEB_BASE + 0xE0 + slot*4`.
  `TlsFree` zeros the TEB slot. `_cooperative_sleep` saves/restores TLS in TEB memory
  around background thread slices so FS:[0xE0+] is always correct for the active thread.
- `PEB+0x18` (ProcessHeap) now populated: `initialize_kernel_structures` takes a
  `process_heap` argument and writes it into PEB memory.
- `TEB_BASE = 0x00320000` and `PEB_BASE = 0x00300000` added as module constants to `_state.py`.

**CriticalSection fix (`kernel32_sync.py`):**
- `EnterCriticalSection`: correctly increments LockCount (+0x04), sets RecursionCount (+0x08)
  and OwningThread (+0x0C) on first acquisition; increments RecursionCount on recursive entry
  by the same thread. Halts loudly if contested (blocked thread — not implemented).
- `LeaveCriticalSection`: decrements RecursionCount; on full release clears OwningThread
  and decrements LockCount. Halts loudly if waiters exist (LockSemaphore signal — not implemented).

**kernel32_handlers.py split:**
- Monolithic `kernel32_handlers.py` (~1295 lines) split into orchestrator (~329 lines)
  + 5 focused sub-modules: `kernel32_memory.py`, `kernel32_sync.py`, `kernel32_locale.py`,
  `kernel32_system.py`, `kernel32_io.py`. All 388 tests pass.

**Tests:** 388 (up from 386; 2 new: PEB ProcessHeap layout, LastError TEB address).

---

## 2026-04-17 — Heap fix, VirtualAlloc accuracy, user32 handlers, hook dispatch

**Progress:**
Game now enters the main message loop (GetMessageA/PeekMessageA). Blocked by
sporadic SDL_QUIT of unknown origin — not user-triggered. All previously queued
issues (GetKeyState, GetSystemMetrics cap, SetActiveWindow, SystemParametersInfoA)
resolved.

**`__free_dbg` patch (`patch_internals.py`):**
- 0x009F6E20 patched to no-op: MSVC debug CRT internal free validates block headers
  that our bump allocator never writes. `__freeptd` (called by `__endthread`) was
  asserting on every thread exit. Consistent with existing `free()` IAT no-op.

**VirtualAlloc accuracy (`kernel32_handlers.py`):**
- `MEM_RESERVE` with non-zero `lp_addr` now honors the requested address instead of
  ignoring it and using the bump allocator. Bump pointer advanced past the reserved
  region to prevent future overlap.
- `MEM_COMMIT` only: range check against all reserved regions instead of exact-base
  lookup. Game's custom allocator commits sub-pages of a block reserved as a whole.

**New user32 handlers (`user32_handlers.py`):**
- `GetKeyState` → 0 (all keys up)
- `GetSystemMetrics` → SM_CXSCREEN/SM_CYSCREEN capped at 1024×768
- `SetActiveWindow` → NULL
- `SystemParametersInfoA` → SPI_GETSCREENSAVEACTIVE (FALSE), SPI_GETWORKAREA
  (0,0,1024,768), TRUE for others
- `SetWindowsHookExA` → real registration in `_winhooks` dict
- `UnhookWindowsHookEx` → removes from `_winhooks`
- `CallNextHookEx` → 0 (no chain)

**Hook dispatch (`user32_handlers.py`):**
- `_dispatch_winhooks`: called from `PeekMessageA` and `GetMessageA` after writing
  MSG struct. Fires WH_GETMESSAGE hooks with (HC_ACTION, PM_REMOVE, lp_msg) and
  WH_KEYBOARD hooks with (HC_ACTION, vk, 0) for WM_KEYDOWN/WM_KEYUP messages.

**Keyboard input pipeline (`window_manager.py`):**
- WM_KEYDOWN/WM_KEYUP/WM_CHAR/WM_MOUSEMOVE/WM_LBUTTONDOWN/WM_LBUTTONUP constants added
- `_sdl_sym_to_vk`: maps SDL keysyms to Win32 VK codes
- SDL_KEYDOWN/SDL_KEYUP now post WM_KEYDOWN/WM_KEYUP to message queue in addition to
  existing dialog-specific handling

**Progress heartbeat (`run_exe.py`):**
- `[alive]` INFO log every 5M `cpu.step()` calls: step count, EIP, virtual time
- Does NOT fire during `GetMessageA` host-sleep (see status.md queued issues)

---

## 2026-04-17 — GDI object table + step-loop performance

**Progress:**
Game now opens a real SDL window ('Motor City Online') and runs further into startup.
Halts at `GetKeyState(VK_CAPITAL)` on tid=1007 — next blocker.

**GDI object table (user32_handlers.py):**
- `_GdiObj` dataclass: kind/color/style/is_stock
- Stock objects pre-populated at registration (WHITE_BRUSH=0 through DC_PEN=19),
  handles 0x2001+fnObject — stable and traceable
- `GetStockObject`: O(1) lookup into `_stock_handles` dict, returns real handle
- `SelectObject`: real per-DC selection tracking (`_dc_selected`), returns previous handle
- `CreateSolidBrush`: allocates dynamic `_GdiObj` entry from counter 0x3001+
- `DeleteObject`: removes dynamic objects; stock objects survive
- DC state initialized in `_alloc_hdc`, cleaned up in `ReleaseDC`/`DeleteDC`

**Performance:**
- `is_valid_eip`: O(N) linear scan → O(1) dict keyed on 4KB page number (~29K entries,
  built once at startup); major win at 123M+ calls per run
- `cpu.step()`: merged `_skip_prefix` — fetch opcode first, handle prefix inline;
  eliminates one wasted memory read per non-prefix instruction (~99% of steps);
  `_clear_prefixes` only called when a prefix was actually set
- Main loop: modulo → countdown counters; removed dead `prev_eip` assignment

**Next blockers (status.md updated):**
1. `GetKeyState` on tid=1007 — return 0 (key not pressed), 2 lines
2. `GetSystemMetrics` returns real display resolution → window too large (cap at 1024×768)

---

## 2026-04-17 — Timer unblock + handler correctness audit

**Progress:**
Game now runs through full startup: login dialog → HTTP auth (200 OK) → authlogin.dll →
options.ini defaults → dx8z.dll/D3D8 init → timer thread created. Halts at
`GetStockObject(BLACK_BRUSH)` in gdi32. Next milestone: GDI object table.

**Timer / scheduler fixes (unblocked _TIMER_waitticks spin):**
- `_run_background_slice` extracted as module-level function in `kernel32_io.py`
- `WaitForSingleObject(INFINITE)` on main thread now drives background threads
  (process-zero pattern) instead of halting emulation
- Timer heartbeat in `run_exe.py` fires every 100K steps: advances `virtual_ticks_ms`,
  invokes due timer callbacks, runs one background slice — unblocks tid=1006 ticks

**Handler correctness audit:**
- `_lclose`: was silently returning 0; now reads handle, closes host fd, returns
  correct value (handle on success, HFILE_ERROR on unknown)
- `advapi32_handlers.py`: five stack reads missing `& 0xFFFFFFFF` mask — fixed
- `SetForegroundWindow`: misleading "pretend it worked" comment replaced with
  explanation (SDL2 owns the window; Win32 focus mechanics don't apply)
- `GetStockObject`: a fake-handle implementation was written and reverted — kept
  as `_halt` until a real GDI object table exists
- Unused imports and dead variable cleaned (ruff)

---

## 2026-04-13 (third pass) — Bitmap rendering for STATIC controls

**What was broken:**
The "Motor City Online" connecting dialog has one STATIC child control whose
`title = "#109"` (parsed from the Win32 resource 0xFFFF ordinal encoding as a
bitmap reference).  `_render_static` was rendering this as `"[#109]"` placeholder
text, which appeared as a visible window on screen.

**Fix:**
- New `tew/api/bitmap_loader.py`:
  - `BitmapInfo` frozen dataclass (width, height, BGR24 pixels)
  - `parse_dib(raw)` — parses BITMAPINFOHEADER, supports 1/4/8/24/32 bpp,
    flips bottom-up rows to top-down
  - `create_sdl_texture(renderer, info)` — converts BGR24 to SDL2 texture
  - `load_bitmap_texture(renderer, bitmap_id, pe_resources)` — high-level loader
- `WindowEntry.bitmap_texture` field — holds SDL_Texture for STATIC bitmap controls
- `WindowManager.set_pe_resources(pe_resources)` — wires PE resources into wm
- `WindowManager.create_dialog` — preloads bitmap textures for STATIC "#N" controls
- `destroy_window` / `shutdown` — call `SDL_DestroyTexture` on bitmap_texture
- `dialog_renderer._render_static` — renders actual texture via `SDL_RenderCopy`;
  falls back to plain grey rectangle (no text) if texture unavailable
- `run_exe.py` — wires `set_pe_resources` after PE load

**Tests:** 18 new tests in `tests/unit/kernel/test_bitmap_loader.py` (all passing).
Total: 386 tests.

**Still open:**
- `bpp:-1` in dx8z OutputDebugString (D3D display mode query returns garbage)
- TIMER_init failure (`__beginthreadex` not handled → timer thread never starts)
- Threads 1004/1005 crash at stub 68 (InterlockedCompareExchange)

---

## 2026-04-13 (second pass) — Bug-fix session: thread crashes + scheduler stability

**Commit**: e4eb34a

**What broke and why:**

Five background threads (Chat Filter, two INet, two more) were all dying every run.
Root causes found via Ghidra + log analysis:

- Threads 1004–1005 hit `TlsSetValue` with an invalid slot index → halt stub fired.
  Fixed: return `FALSE` (Win32 behaviour) instead of halting.
- Threads 1001–1003 hit `EnterCriticalSection` and triggered `cpu.halted` via an
  internal Python exception (`ValueError` from out-of-bounds `memory.read32`).
  Root cause: corrupted ESP. Now the crash message includes `cpu.last_error` so
  the actual fault address will be visible on next run.
- After all threads died, the main thread entered a Sleep() loop. Each Sleep() tried
  to schedule another thread, which called Sleep(), which recursed into another
  `_run_thread_slice` — Python stack overflow ("maximum recursion depth exceeded").
  Fixed: `_cooperative_sleep` / `_cooperative_sleep_ex` guard on `state.is_running_thread`.

**Other fixes:**

- `MessageBoxA` returned `IDOK=1` for `MB_YESNO` — the fullscreen-prompt branch in
  `Platform_SysStartUp` checks `if (result == 7)` (IDNO) for windowed mode. `1` is
  neither yes nor no, so the game's mode flag was indeterminate. Now returns `IDYES=6`.
- `GetModuleHandleA("KERNEL32")` returned NULL — stub-only DLLs have no `LoadedDLL`
  entry. Added `Win32Handlers.get_stub_dll_handle()` returning the first handler's
  trampoline address as a stable non-NULL handle. 11 unit tests added.
- `GetDeviceCaps` had no logging — calls were invisible in `LOG_CATEGORIES=handlers`.
  Added hdc + capability-name debug line. Confirmed BITSPIXEL returns 32 correctly.
- `CreateWindowExA` with `MAKEINTATOM(109)` did a name-table lookup for `"#109"` which
  was never inserted. Fixed to look up by atom value in `_classes` directly.

**Ghidra analysis this session:**

- `Platform_SysStartUp` (0x006b13b0): full decompile — fullscreen prompt, dx8z load,
  `_THRASH_setvideomode`, window init sequence, thread creation order
- `func_0x0040490d` → JMP → `GameSetup_LoadOptions` (0x0055d280): NOT window creation
- `_THRASH_createwindow` (dx8z.dll 0x60001760): internal dx8z allocation, no Win32
- `FUN_60003920` (dx8z.dll): D3D device enumeration, BITSPIXEL check, COM vtable calls

## 2026-04-12 — v0.9.0 — CRT init session: 10K → 6.77M steps

**Step count**: 10,765 → 6,772,724 (657× increase in one session).

Game is now printing OutputDebugString messages from authlogin/INET startup code:
`Filter thread started` and `Creating INET Message Object`.

**New modules:**
- `tew/api/char_type.py` — CT_CTYPE1 lookup table, WideMemory Protocol, GetStringTypeArgs DTO
- `tew/api/lc_map.py` — LCMapFlags IntFlag enum, LCMapStringArgs DTO, case conversion
- `tew/api/win32_errors.py` — Win32Error IntEnum (winerror.h constants)
- `tew/api/ini_file.py` — full INI parser + reader + writer (parse_ini, read/write_profile_string/section)
- `tew/api/version_handlers.py` — GetFileVersionInfoSizeA family (returns 0: no version resource)
- `tew/api/wininet_handlers.py` — full WinINet HTTP stack via http.client; forwarding to localhost

**Blockers cleared (in order):**
1. `GetStringTypeW` — CT_CTYPE1 lookup table for Unicode classification
2. `LCMapStringW` — LCMAP_LOWERCASE/UPPERCASE via codepoint mapping
3. `GetModuleFileNameA` — CRTState.exe_path + reverse_translate_path()
4. `HeapValidate` — checks heap_handles set, returns TRUE
5. `GetLastError` / `SetLastError` — last_error field on CRTState
6. `GetPrivateProfileStringA/IntA` — real INI parsing; file read via find_file_ci
7. `WritePrivateProfileStringA/SectionA` — real INI file write
8. `GetFileVersionInfoSizeA/A`, `VerQueryValueA` — version_handlers.py
9. Full WinINet stack — InternetOpen/Connect/HttpSend/Read/QueryInfo/Close
10. `_initterm` / `_initterm_e` — **calls back into guest CPU**; real static initializer dispatch via THREAD_SENTINEL
11. `VirtualProtect` — flat memory model; returns PAGE_EXECUTE_READWRITE, TRUE
12. `GlobalMemoryStatus` — plausible 256 MB values
13. `SetEnvironmentVariableA/W`, `GetEnvironmentVariableA/W` — module-level _env_vars dict

**Infrastructure improvements:**
- `flush=True` on all logger print() calls — fixes INFO/ERROR ordering in piped output
- `diagnose_halt()` in exception_diagnostics.py — prints registers + 16-slot stack walk on any halt
- `diagnose_halt` wired into run_exe.py post-loop alongside existing diagnose_fault

**Tests:**
- 302 tests, all passing
- New test files: test_char_type.py (46), test_lc_map.py (34), test_module_filename.py (14),
  test_heap.py (6), test_last_error.py (15), test_ini_file.py (47)

**Next blocker:** `DuplicateHandle`

---

## 2026-04-12 — v0.8.0 — GetStringTypeW implemented

**Blocker cleared** — `GetStringTypeW` now has a real implementation.

**New files:**
- `tew/api/char_type.py` — CT_CTYPE1 lookup table for ASCII (U+0000–U+007F),
  `Ctype1` IntFlag enum, `GetStringTypeArgs` frozen dataclass (DTO),
  `WideMemory` Protocol, `classify_ctype1()`, `classify_wide_string()`
- `tests/unit/kernel/test_char_type.py` — 46 tests; all pass

**Changed:**
- `kernel32_handlers.py` — `GetStringTypeW` handler replaced; halts loudly
  if called with CT_CTYPE2 or CT_CTYPE3 (not needed by MCO, implement on demand)

**Design notes:**
- CT_CTYPE2 and CT_CTYPE3 are NOT implemented — handler halts if called.
  This is intentional: fail loudly rather than return silent garbage.
- `WideMemory` Protocol makes `classify_wide_string` testable without the
  full emulator setup — `Memory` satisfies it structurally.

---

## 2026-04-11 — v0.7.0 — Code hygiene session

**No step count change** — 10,765 steps, same GetStringTypeW halt.

**Structural fixes:**
- `SavedCPUState` moved from `tew/api/_state.py` to `tew/hardware/cpu.py`
  (it's a CPU type; hardware layer shouldn't depend on api layer)
- `save_state()` / `restore_state()` added as methods on `CPU`
  (they touch CPU internals directly; belong on the class)
- Duplicate `_save_cpu_state` / `_restore_cpu_state` functions removed from
  `kernel32_handlers.py` and `kernel32_io.py` — was an exact copy-paste

**Linter / tooling:**
- `ruff` installed and configured in `pyproject.toml`
- `requires-python` bumped to `>=3.12` (venv runs 3.13; fixes f-string
  backslash escape syntax errors that were latent on 3.11)
- 48 unused imports removed (auto-fix)
- `_vt` in `oleaut32_handlers.py` prefixed with `_` to signal intentional discard
- E701/E702 (compact one-liners in opcode tables) suppressed globally —
  intentional style for condition code dispatch and paired flag sets

**Removed:**
- `main.py` — predated the port, `run_exe.py` is the entry point

**Deferred (documented in memory):**
- Split `_state.py` into DTOs vs runtime state
- Split large handler files by concept (heap, file, thread, sync)
  rather than by DLL name

---

## 2026-04-01 — v0.6.0 — Initial Python port committed

Full Python port of the TypeScript emulator. Runs 10,765 steps through
CRT initialization, halts at `GetStringTypeW`. Includes:
- CPU core, full opcode implementations, x87 FPU
- PE/DLL loader with base relocations and IAT patching
- Win32/CRT/D3D8/User32/OleAut32/Advapi32 handler stubs
- Cooperative thread scheduler
- Unit test suite (140 tests)
