# Emulator Session Status

## Target
MCity_d.exe — MSVC debug build, Win32, 32-bit. Pentium II instruction set.

## Source of truth
Intel 80386 Programmer's Reference Manual, 1986
Path: ~/Documents/i386.pdf (421 pages)

## Current state
Session 2026-04-13 (third pass). No halt — game runs indefinitely.

Game reaches `Platform_SysStartUp`, handles the fullscreen-prompt MessageBox,
loads dx8z.dll, calls GetDeviceCaps (BITSPIXEL → 32), then enters a cooperative
sleep loop after WSAStartup + CreateEventA. All background threads (Chat Filter,
two INet threads) die from TLS or memory faults before the main loop can proceed.

## Current blockers
1. **Threads 1004/1005 crash at stub 68** — `InterlockedCompareExchange`.
   Threads 1001–1003 are now running (EnterCriticalSection crash was fixed in e4eb34a).
2. **TIMER_init failure** — `_TIMER_init` calls `_THREAD_create` → `__beginthreadex`
   (not in msvcrt_handlers) → timer thread never starts → abortmessage after timeout.
   Fix requires: `__beginthreadex` handler + `WaitForMultipleObjectsEx` event signaling.
3. **`bpp:-1` in OutputDebugString** — dx8z.dll D3D display mode query returns garbage
   values (width/height = 0xCCCCCCCC, bpp = -1). Needs D3D adapter display mode stub
   to return plausible values.

## Architecture discovered
- Game does NOT call D3D8 directly.
- Rendering path: Game → THRASH API (dx8z.dll) → D3D8 (fake COM)
- dx8z.dll is the real D3D8 backend; game calls _THRASH_* functions
- WinINet connects to localhost:443 (HTTPS)
- authlogin.dll reads AuthLoginServer from registry (localhost) — works
- fullscreen prompt: MessageBoxA with MB_YESNO=4; check was `if (result == 7)` for
  windowed mode — we now return IDYES=6 correctly

## Cooperative scheduler — key facts
- Sleep() from main thread → _cooperative_sleep → _run_thread_slice
- Background threads that call Sleep() during their slice must NOT recurse
  into another _run_thread_slice (Python stack overflow). Guard added.
- Thread stack: 0x08000000 + tid*0x40000, 256KB each, within 2GB memory.
- Thread crash path: cpu.step() catches Python exceptions → cpu.halted=True.
  cpu.last_error now logged in crash message.

## Functions added this session (2026-04-13 second pass)
1. `Win32Handlers.get_stub_dll_handle()` — stable handle for stub-only DLLs
2. `MessageBoxA` — correct return values for all button types (IDYES, IDNO, etc.)
3. `GetDeviceCaps` — added hdc + cap-name debug logging
4. `CreateWindowExA` — fixed atom lookup for MAKEINTATOM pattern
5. `TlsSetValue/GetValue/Free` — return FALSE/0 instead of halting on bad slot
6. `_cooperative_sleep` / `_cooperative_sleep_ex` — reentrance guard
7. Thread crash message — now includes cpu.last_error

## Functions/modules added this session (2026-04-13 third pass)
1. `tew/api/bitmap_loader.py` — `parse_dib`, `BitmapInfo`, `create_sdl_texture`,
   `load_bitmap_texture`; full Win32 DIB parsing (1/4/8/24/32 bpp)
2. `WindowEntry.bitmap_texture` — SDL_Texture for STATIC bitmap controls
3. `WindowManager.set_pe_resources` — wires PE resources into the window manager
4. `WindowManager.create_dialog` — preloads bitmap textures for STATIC "#N" controls
5. `dialog_renderer._render_static` — renders bitmap texture; grey rect as fallback

## Test suite
386 tests (all passing).
New: tests/unit/kernel/test_bitmap_loader.py — 18 tests.

## Next investigation
- Run with LOG_CATEGORIES=thread,handlers to see new thread crash detail
  (cpu.last_error should show what memory address caused the fault)
- Check if threads 1004–1005 survive with TLS fix
- Find who registers the window class with atom 109 (LOG_CATEGORIES=window)
- Once threads survive: what does the main thread wait on (which event/mutex)?
