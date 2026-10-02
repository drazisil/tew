"""run_exe.py — Boot and run a Win32 PE executable in the x86-32 emulator.

Usage:
    python run_exe.py [path/to/game.exe]
    python run_exe.py --install-dir /path/to/game [--exe MCity_d.exe]

If no path is given, reads 'exePath' from emulator.json in the current directory.

Environment variables:
    LOG_LEVEL=trace|debug|info|warn|error   (default: info)
    LOG_CATEGORIES=startup,cpu,handlers,...  (default: all)
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import sys
import time
from os.path import dirname

from tew.api._state import HEAP_BASE, THREAD_STACK_BASE, EmulatorConfig
from tew.api.crt_handlers import patch_crt_internals, register_crt_handlers
from tew.api.kernel32_handlers import _invoke_dependency_dllmain
from tew.api.nt_handlers import register_nt_handlers
from tew.api.pe_resources import PEResources
from tew.api.win32_handlers import (
    HANDLER_BASE,
    HANDLER_SIZE,
    MAX_HANDLERS,
    Win32Handlers,
)
from tew.automation import AutomationEventEmitter, EventLogger
from tew.automation.gui_events import install_gui_events, skip_noisy_classes
from tew.hardware.cpu_zig import EBP, ESP, FatalHaltError
from tew.hardware.cpu_zig import ZigCPU as CPU
from tew.hardware.memory import Memory
from tew.kernel.exception_diagnostics import diagnose_fault, diagnose_halt
from tew.kernel.kernel_structures import KernelStructures
from tew.kernel.seh import STATUS_ACCESS_VIOLATION, dispatch_exception
from tew.logger import WARN, configure_logger, logger, set_thread_id_provider
from tew.pe.exe_file import EXEFile

# ── Parse arguments ───────────────────────────────────────────────────────────

_parser = argparse.ArgumentParser(
    description="Run a Win32 PE executable in the x86-32 emulator",
    add_help=True,
)
_parser.add_argument(
    "positional_exe", nargs="?", metavar="exe",
    help="Path to the .exe to run (overrides emulator.json exePath)",
)
_parser.add_argument(
    "--install-dir", metavar="DIR",
    help="Game install directory. Sets C:\\ path mapping and working directory. "
         "Replaces the need for pathMappings in emulator.json.",
)
_parser.add_argument(
    "--exe", metavar="EXE", dest="exe_flag",
    help="Exe filename or relative path within --install-dir (e.g. MCity_d.exe or "
         "3dSetup/3DSetup.exe). Ignored if --install-dir is not given.",
)
_args = _parser.parse_args()

# ── Resolve install dir, config, and exe path — all before any chdir ─────────

_repo_dir = os.getcwd()

# Load emulator.json from the repo dir for baseline config + fallback exe path.
_cfg: dict = {}
try:
    with open(os.path.join(_repo_dir, "emulator.json"), "r", encoding="utf-8") as _f:
        _cfg = json.load(_f)
except Exception:
    logger.error("startup", f"Failed to load emulator.json from {_repo_dir} -- using defaults")
    raise SystemExit(f"emulator.json not found in {_repo_dir} -- run from the repo root or pass --install-dir")

install_dir: str | None = None
if _args.install_dir:
    install_dir = os.path.abspath(_args.install_dir)
    if not os.path.isdir(install_dir):
        raise SystemExit(f"--install-dir does not exist: {install_dir}")

# Build EmulatorConfig: --install-dir overrides C:\\ mapping; otherwise use emulator.json.
if install_dir:
    _path_mappings: dict[str, str] = {"c:/": install_dir.rstrip("/") + "/"}
else:
    _raw = _cfg.get("pathMappings", {})
    _path_mappings = {
        k.replace("\\", "/").lower(): v
        for k, v in _raw.items()
        if not k.startswith("_")
    }
_emulator_config = EmulatorConfig(
    path_mappings=_path_mappings,
    interactive_on_missing_file=_cfg.get("interactiveOnMissingFile") is True,
)

# Resolve exe_path.
exe_path: str = ""
if _args.positional_exe:
    exe_path = _args.positional_exe
elif install_dir:
    if _args.exe_flag:
        exe_path = os.path.join(install_dir, _args.exe_flag)
    else:
        # Fall back to the basename from emulator.json exePath, looked up in install_dir.
        _json_exe = _cfg.get("exePath", "")
        if _json_exe:
            exe_path = os.path.join(install_dir, os.path.basename(_json_exe))
        else:
            _exes = [f for f in os.listdir(install_dir) if f.lower().endswith(".exe")]
            if len(_exes) == 1:
                exe_path = os.path.join(install_dir, _exes[0])
            elif _exes:
                raise SystemExit(
                    f"Multiple .exe files in {install_dir}: {_exes}\n"
                    f"Use --exe to specify one."
                )
            else:
                raise SystemExit(f"No .exe found in {install_dir}")
else:
    exe_path = _cfg.get("exePath", "")

if not exe_path:
    raise SystemExit(
        "No exe path specified. Pass as a positional argument, use --install-dir/--exe, "
        "or set 'exePath' in emulator.json."
    )

# chdir to install_dir so relative game file writes land there, not in the repo.
if install_dir:
    os.chdir(install_dir)

# ── Load PE ───────────────────────────────────────────────────────────────────

logger.info("startup", f"=== Loading PE File: {exe_path} ===")
exe = EXEFile(exe_path, [])

logger.debug("startup", f"Entry point RVA: 0x{exe.optional_header.address_of_entry_point:x}")
logger.debug("startup", f"Image base: 0x{exe.optional_header.image_base:x}")
logger.debug("startup", f"Sections: {len(exe.section_headers)}")

entry_rva = exe.optional_header.address_of_entry_point
entry_section = next(
    (s for s in exe.section_headers
     if s.virtual_address <= entry_rva < s.virtual_address + s.virtual_size),
    None,
)
logger.debug("startup", f"Entry point in section: {entry_section.name if entry_section else 'NOT FOUND'}")

# ── Create emulator ───────────────────────────────────────────────────────────
# 2 GB flat address space (Linux lazily commits pages; physical RAM usage is
# proportional to what the game actually writes, not the reservation size).

mem = Memory(2 * 1024 * 1024 * 1024)
cpu = CPU(mem)
# Real Windows never maps the first 64KB. The game's anti-debug self-test
# (_CLayer_DetectDebugger) relies on a real fault there. Off by default in the
# Zig core because bare unit-test CpuStates use tiny address-0 buffers.
cpu.enable_null_page_guard()

kernel_structures = KernelStructures(mem)
cpu.kernel_structures = kernel_structures

exe.import_resolver.set_memory(mem)

# Address space owned before any DLL loads (heap, thread stacks), so the loader
# rebases a DLL whose preferred base falls inside it, as Windows does (DAO350.DLL
# prefers 0x04470000, inside the heap). Thread stacks grow up from
# THREAD_STACK_BASE with no cap, so reserve up to the loader's fallback range.
_img_base = exe.optional_header.image_base
exe.import_resolver.reserve_address_range(
    "exe image", _img_base, _img_base + exe.optional_header.size_of_image - 1)
exe.import_resolver.reserve_address_range(
    "API stubs", HANDLER_BASE, HANDLER_BASE + MAX_HANDLERS * HANDLER_SIZE - 1)
exe.import_resolver.reserve_address_range("heap", HEAP_BASE, THREAD_STACK_BASE - 1)
exe.import_resolver.reserve_address_range("thread stacks", THREAD_STACK_BASE, 0x0FFFFFFF)

# DLL search paths: application directory first, as the Windows loader does.
# Keep them inside ~/.emu32 (period-correct DLLs); copy a DLL in there rather
# than pointing at another directory.
exe.import_resolver.add_dll_search_path(dirname(exe_path))
# Real COM servers (oleaut32.dll, dao350.dll, ...). Must be on the search path
# before build_iat_map: it loads every directly-imported DLL once and caches the
# result, so a DLL not found then resolves to a fatal-halt stub for good.
exe.import_resolver.add_dll_search_path("/home/drazisil/.emu32/WINDOWS/System32")

# Statically-imported real DLLs (and their dependencies) need DllMain too, e.g.
# oleaut32's TlsAlloc, without which SysAllocString fails. Collect them here and
# run DllMain later: their own IAT slots aren't patched yet at this point. The
# list comes out in dependency-before-dependent order.
_pending_dllmain_dlls: list = []
exe.import_resolver.build_iat_map(
    exe.import_table, exe.optional_header.image_base, _pending_dllmain_dlls.append
)

# ── Register Win32 stubs ──────────────────────────────────────────────────────

win32_handlers = Win32Handlers(mem)
_dll_loader_ref = exe.import_resolver.get_dll_loader()
crt_state = register_crt_handlers(
    win32_handlers, mem, _dll_loader_ref,
    config=_emulator_config, registry_dir=_repo_dir,
)
crt_state.exe_path = exe_path   # used by GetModuleFileNameA
set_thread_id_provider(crt_state.tls_current_thread_id)

# The CRT's AppendToCRTLeaksFile appends to C:\memleaksCRT.txt; truncate it per
# run so it holds only this run's report.
_memleaks_path = crt_state.translate_windows_path("C:\\memleaksCRT.txt")
if os.path.exists(_memleaks_path):
    os.remove(_memleaks_path)

# Attach PE resources so dialog templates and bitmap controls can be loaded
with open(exe_path, "rb") as _f:
    _pe_resources = PEResources(_f.read())
crt_state.pe_resources = _pe_resources
crt_state.window_manager.set_pe_resources(_pe_resources)

# ── Unattended boot: auto-answer the two startup prompts (login Continue, full
# ── screen No). The untitled splash dialog (resource 106) closes by itself.

_LOGIN_CONTINUE_ID = 0x0001
_IDNO = 7

def _auto_click_login_continue(wm, dlg_hwnd):
    """Dialog 114 ("Motor City Online Login"): username/password are
    already sourced from registry.json by the game itself, so only the
    Continue click is needed."""
    entry = wm.get_window(dlg_hwnd)
    if entry is None or entry.title != "Motor City Online Login":
        wm.set_dialog_step_hook(_auto_click_login_continue)
        return
    wm.click_control(dlg_hwnd, _LOGIN_CONTINUE_ID)

def _auto_decline_fullscreen_prompt(caption, text, u_type):
    """MB_YESNO "Do you want to run Motor City Online full screen?"
    (FUN_006b13b0) -- default to windowed."""
    if "full screen" in text:
        return _IDNO
    return None

# TEW_NO_AUTO=1 turns off every baked-in input: these two and the
# persona-select START click below, for driving the game by hand.
_TEW_NO_AUTO = os.environ.get("TEW_NO_AUTO", "") not in ("", "0")
if not _TEW_NO_AUTO:
    crt_state.window_manager.set_dialog_step_hook(_auto_click_login_continue)
    crt_state.window_manager.set_messagebox_hook(_auto_decline_fullscreen_prompt)

# ── Click injection ─────────────────────────────────────────────────────────
# Pushes real SDL events through window_manager's pump, the same path a real
# click takes (WM_* messages and DirectInput state). Down and up are separate
# timed events: the game polls DirectInput ~every 385ms, so a click needs a
# real hold time or no poll sees it.
#   TEW_CLICK_AT=<x>,<y>        window-relative physical pixels
#   TEW_CLICK_AFTER_SEC=<secs>  wall-clock seconds after start
#   TEW_CLICK_HOLD_SEC=<secs>   hold time (default 0.5)
#   TEW_CLOSE_AFTER_SEC=<secs>  push SDL_WINDOWEVENT_CLOSE after that long
_TEW_CLOSE_AFTER_SEC = os.environ.get("TEW_CLOSE_AFTER_SEC")
_close_injected = False

_TEW_CLICK_AT = os.environ.get("TEW_CLICK_AT")
_TEW_CLICK_AFTER_SEC = os.environ.get("TEW_CLICK_AFTER_SEC")
_TEW_CLICK_HOLD_SEC = float(os.environ.get("TEW_CLICK_HOLD_SEC", "0.5"))
_click_down_injected = False
_click_up_injected = False
_click_down_wall_time: float | None = None
_click_start_wall_time = time.monotonic()

#   TEW_CLICK_PREMOVE_SEC=<secs>  move onto the target this long before the
#                                 button-down (default 1.0) so the game sees hover
_TEW_CLICK_PREMOVE_SEC = float(os.environ.get("TEW_CLICK_PREMOVE_SEC", "1.0"))
_click_premove_injected = False

# Click when the game logs that a screen is ready instead of at a fixed time
# (startup time varies a lot). The click happens TEW_CLICK_WHEN_DELAY_SEC
# (default 30) after the text appears: dialogs accept input a while after
# their log line. Requires TEW_CLICK_AT; exclusive with TEW_CLICK_AFTER_SEC.
#   TEW_CLICK_WHEN_FILE=<path>   e.g. ~/.emu32/MCity/MCity_Log.txt
#   TEW_CLICK_WHEN_TEXT=<text>   e.g. "Done Getting Personas"
from tew.file_trigger import FileTextTrigger

_TEW_CLICK_WHEN_FILE = os.environ.get("TEW_CLICK_WHEN_FILE")
_TEW_CLICK_WHEN_TEXT = os.environ.get("TEW_CLICK_WHEN_TEXT")
_TEW_CLICK_WHEN_DELAY_SEC = float(os.environ.get("TEW_CLICK_WHEN_DELAY_SEC", "30.0"))
_click_file_trigger: FileTextTrigger | None = None
if _TEW_CLICK_WHEN_FILE or _TEW_CLICK_WHEN_TEXT:
    if not (_TEW_CLICK_WHEN_FILE and _TEW_CLICK_WHEN_TEXT and _TEW_CLICK_AT):
        raise SystemExit(
            "TEW_CLICK_WHEN_FILE, TEW_CLICK_WHEN_TEXT and TEW_CLICK_AT must all be set together")
    if _TEW_CLICK_AFTER_SEC:
        raise SystemExit(
            "set either TEW_CLICK_AFTER_SEC (time-based) or TEW_CLICK_WHEN_FILE/TEXT (file-based), not both")
    _click_file_trigger = FileTextTrigger(
        os.path.expanduser(_TEW_CLICK_WHEN_FILE), _TEW_CLICK_WHEN_TEXT)

# Baked-in clicks (unless TEW_NO_AUTO or any explicit TEW_CLICK_* setup), run
# as a chain. Positions are logical 800x600 coordinates (stock GUI set),
# converted to physical pixels when each step fires.
#   1. persona-select START (389,444): 30s after MCity_Log.txt "Done Getting Personas"
#   2. Mayor's letter CONTINUE (399,540): 30s after stdout.txt "New mail IDs detected!"
#   3. Screen Tips popup X (563,215): 30s after step 2
#   4. OK on the notice that follows (398,335): 30s after step 3
#   5. Racing button in the top nav bar (444,13): 30s after step 4. Lframe2 bar
#      at [0,0] on scn.home, but3_race at [385,-1] 118x29 -> centre (444,13)
#   6. Test Drive, the last item of the Racing menu (446,141): 30s after step 5.
#      The menu is built in code (LFrame_MakeRaceMenu), so the position comes
#      from a screenshot of the open menu, not the GUI files
#   7. TEST DRIVE (the <OK> button) on the TestDrive dialog (274,396): the dialog
#      is re-centred by layout to [212,180] 375x239, <OK> at [0,209] 124x30 (centre y=180+209+15=404; 396 is the visual centre per screenshot)
# The server always reports a first visit, so all four screens show every run.
# Steps 3-4 use a fixed delay: a file trigger only reads past the size it saw
# at startup, and misses text once the rewritten stdout.txt has grown past it.
_auto_click_steps: list[tuple[FileTextTrigger | None, str | None, str | None, tuple[int, int]]] = []
if not _TEW_NO_AUTO and not (_TEW_CLICK_AT or _TEW_CLICK_AFTER_SEC or _click_file_trigger):
    for _win_path, _text, _xy in (
        ("C:\\MCity\\MCity_Log.txt", "Done Getting Personas", (389, 444)),
        ("C:\\MCity\\stdout.txt", "New mail IDs detected!", (399, 540)),
        (None, None, (563, 215)),
        (None, None, (398, 335)),
        (None, None, (444, 13)),
        (None, None, (446, 141)),
        (None, None, (274, 396)),
    ):
        if _win_path is None:
            _auto_click_steps.append((None, None, None, _xy))
            continue
        _path = crt_state.translate_windows_path(_win_path)
        _auto_click_steps.append((FileTextTrigger(_path, _text), _path, _text, _xy))
_auto_click_xy: tuple[int, int] | None = None


def _arm_next_auto_click() -> bool:
    """Make the next baked-in step the active file trigger. A step without a
    trigger is instead due _TEW_CLICK_WHEN_DELAY_SEC from now; returns True
    for those so the caller schedules the click itself."""
    global _click_file_trigger, _TEW_CLICK_WHEN_FILE, _TEW_CLICK_WHEN_TEXT, _auto_click_xy
    _click_file_trigger, _TEW_CLICK_WHEN_FILE, _TEW_CLICK_WHEN_TEXT, _auto_click_xy = \
        _auto_click_steps.pop(0)
    return _click_file_trigger is None


if _auto_click_steps:
    _arm_next_auto_click()

# 2026-09-18: out-of-range x87 FIST/FISTP stores are silent on real hardware
# (integer indefinite) but here they mean upstream float math produced
# NaN/Inf/garbage -- see TODO.md's screen.c(475) entry. Reported as they happen.
_fist_seen = 0

# Double-click injection (the persona list accepts on double-click). The game
# needs two down-edges within GetDoubleClickTime (500ms) while polling every
# ~385ms, so a single attempt is timing-dependent; repeat it.
#   TEW_DBLCLICK_AT=<x>,<y>          window-relative physical pixels
#   TEW_DBLCLICK_AFTER_SEC=<secs>    wall-clock seconds after start
#   TEW_DBLCLICK_HOLD_SEC=<secs>     hold per click (default 0.15)
#   TEW_DBLCLICK_GAP_SEC=<secs>      gap between the two clicks (default 0.05)
#   TEW_DBLCLICK_REPEAT=<n>          attempts (default 3)
#   TEW_DBLCLICK_RETRY_SEC=<secs>    seconds between attempts (default 1.0)
_TEW_DBLCLICK_AT = os.environ.get("TEW_DBLCLICK_AT")
_TEW_DBLCLICK_AFTER_SEC = os.environ.get("TEW_DBLCLICK_AFTER_SEC")
_TEW_DBLCLICK_HOLD_SEC = float(os.environ.get("TEW_DBLCLICK_HOLD_SEC", "0.15"))
_TEW_DBLCLICK_GAP_SEC = float(os.environ.get("TEW_DBLCLICK_GAP_SEC", "0.05"))
_TEW_DBLCLICK_REPEAT = int(os.environ.get("TEW_DBLCLICK_REPEAT", "3"))
_TEW_DBLCLICK_RETRY_SEC = float(os.environ.get("TEW_DBLCLICK_RETRY_SEC", "1.0"))
# State machine steps per attempt: 0=idle/waiting, 1=click1 down pushed,
# 2=click1 up pushed, 3=click2 down pushed, 4=click2 up pushed (attempt done)
_dblclick_attempt = 0
_dblclick_step = 0
_dblclick_step_wall_time: float | None = None

# On-demand click: write "x,y[,hold_sec[,premove_sec]]" (physical pixels) to the
# trigger file below, e.g. `echo "774,982,2.0" > /tmp/tew_click_trigger`. The
# file is consumed on read; a new one waits until the in-flight click is done.
# hold_sec defaults to 0.5, premove_sec to 0.3.
_TEW_MANUAL_CLICK_TRIGGER = os.environ.get("TEW_MANUAL_CLICK_TRIGGER", "/tmp/tew_click_trigger")
_manual_click_xy: tuple[int, int] | None = None
_manual_click_hold_sec: float = 0.5
_manual_click_premove_sec: float = 0.3
_manual_click_step = 0  # 0=idle, 1=premove pushed (waiting), 2=down pushed (waiting for hold)
_manual_click_step_wall_time: float | None = None

# Live logging change: write "level,categories" (either half optional) to the
# log trigger file.
_TEW_LOG_TRIGGER = os.environ.get("TEW_LOG_TRIGGER", "/tmp/tew_log_trigger")

# Pause/step: write "pause"/"resume" to the pause trigger; while paused, write
# a step count (blank = 1) to the step trigger. Other triggers keep working.
_TEW_PAUSE_TRIGGER = os.environ.get("TEW_PAUSE_TRIGGER", "/tmp/tew_pause_trigger")
_TEW_STEP_TRIGGER = os.environ.get("TEW_STEP_TRIGGER", "/tmp/tew_step_trigger")
_tew_paused = False


def _get_click_sdl_window_id():
    import sdl2

    import tew.api.d3d8._state as _d3d8_state

    entry = crt_state.window_manager.get_window(_d3d8_state._vk_hwnd)
    if entry is None or entry.sdl_window is None:
        logger.error("startup",
            f"[click] no SDL window for hwnd=0x{_d3d8_state._vk_hwnd:x} -- cannot inject click")
        return None
    return sdl2.SDL_GetWindowID(entry.sdl_window)


def _inject_window_close() -> None:
    """Push a real SDL_WINDOWEVENT_CLOSE, exactly what a real click on the
    OS window's own close (X) button produces -- exercises the same
    `_handle_sdl_event`/WM_CLOSE path window_manager.py already has,
    landing on FUN_00780550 (real guest code) -> PostQuitMessage(0), NOT
    any GUI dialog Cancel path. Added 2026-09-14 after Molly's own
    recollection that both times she saw the persona-select dialog
    dismiss and the game later fault, closing the window (her only click)
    was the trigger -- testing whether that's a real causal link or
    coincidence."""
    import sdl2

    win_id = _get_click_sdl_window_id()
    if win_id is None:
        return

    close = sdl2.SDL_Event()
    close.type = sdl2.SDL_WINDOWEVENT
    close.window.windowID = win_id
    close.window.event = sdl2.SDL_WINDOWEVENT_CLOSE
    sdl2.SDL_PushEvent(ctypes.byref(close))

    logger.always(WARN, "startup", "[close] pushed real SDL_WINDOWEVENT_CLOSE")


def _inject_mouse_motion(rel_x: int, rel_y: int) -> None:
    import sdl2

    win_id = _get_click_sdl_window_id()
    if win_id is None:
        return

    motion = sdl2.SDL_Event()
    motion.type = sdl2.SDL_MOUSEMOTION
    motion.motion.windowID = win_id
    motion.motion.which = 0
    motion.motion.state = 0
    motion.motion.x = rel_x
    motion.motion.y = rel_y
    motion.motion.xrel = 0
    motion.motion.yrel = 0
    sdl2.SDL_PushEvent(ctypes.byref(motion))

    logger.always(WARN, "startup",
        f"[click] pushed real SDL motion-only to window-relative ({rel_x},{rel_y})")


def _inject_click_down(rel_x: int, rel_y: int, hold_sec_for_log: float = _TEW_CLICK_HOLD_SEC) -> None:
    import sdl2

    win_id = _get_click_sdl_window_id()
    if win_id is None:
        return

    motion = sdl2.SDL_Event()
    motion.type = sdl2.SDL_MOUSEMOTION
    motion.motion.windowID = win_id
    motion.motion.which = 0
    motion.motion.state = 0
    motion.motion.x = rel_x
    motion.motion.y = rel_y
    motion.motion.xrel = 0
    motion.motion.yrel = 0
    sdl2.SDL_PushEvent(ctypes.byref(motion))

    down = sdl2.SDL_Event()
    down.type = sdl2.SDL_MOUSEBUTTONDOWN
    down.button.windowID = win_id
    down.button.which = 0
    down.button.button = sdl2.SDL_BUTTON_LEFT
    down.button.state = sdl2.SDL_PRESSED
    down.button.clicks = 1
    down.button.x = rel_x
    down.button.y = rel_y
    sdl2.SDL_PushEvent(ctypes.byref(down))

    logger.always(WARN, "startup",
        f"[click] pushed real SDL button-down at window-relative ({rel_x},{rel_y}), "
        f"holding for {hold_sec_for_log}s")


def _inject_click_up(rel_x: int, rel_y: int) -> None:
    import sdl2

    win_id = _get_click_sdl_window_id()
    if win_id is None:
        return

    up = sdl2.SDL_Event()
    up.type = sdl2.SDL_MOUSEBUTTONUP
    up.button.windowID = win_id
    up.button.which = 0
    up.button.button = sdl2.SDL_BUTTON_LEFT
    up.button.state = sdl2.SDL_RELEASED
    up.button.clicks = 1
    up.button.x = rel_x
    up.button.y = rel_y
    sdl2.SDL_PushEvent(ctypes.byref(up))

    logger.always(WARN, "startup",
        f"[click] pushed real SDL button-up at window-relative ({rel_x},{rel_y})")


# `timeout` sends SIGTERM; handle it so SDL shuts down instead of leaving an
# orphaned window. os._exit() for the same reason as at the end of the run.
def _handle_termination_signal(signum, frame):
    logger.error("startup", f"Received signal {signum} -- shutting down SDL2 before exit")
    try:
        crt_state.window_manager.shutdown()
    except Exception as e:
        logger.error("startup", f"SDL2 shutdown during signal handling failed: {e}")
    sys.stdout.flush()
    os._exit(1)

signal.signal(signal.SIGTERM, _handle_termination_signal)
signal.signal(signal.SIGINT, _handle_termination_signal)

win32_handlers.install(cpu)
register_nt_handlers(win32_handlers.nt_dispatcher)

# ── Load sections ─────────────────────────────────────────────────────────────

logger.info("startup", "=== Loading Sections ===")
total_loaded = 0
for section in exe.section_headers:
    vaddr = exe.optional_header.image_base + section.virtual_address
    logger.info(
        "startup",
        f"  {section.name:<8} @ 0x{vaddr:08x}"
        f" (raw:{len(section.data)} virt:{section.virtual_size})",
    )
    if section.data:
        mem.load(vaddr, section.data)
        total_loaded += len(section.data)
    if section.virtual_size > len(section.data):
        uninit = section.virtual_size - len(section.data)
        logger.debug("startup", f"    Note: {uninit} bytes uninitialized (auto-zeroed)")

logger.debug("startup", f"Total loaded: {total_loaded} bytes")

# ── Write IAT entries and patch CRT internals ─────────────────────────────────

exe.import_resolver.write_iat_handlers(
    mem, exe.optional_header.image_base, exe.import_table, win32_handlers
)
patch_crt_internals(win32_handlers, mem, crt_state)

# ── Set up initial CPU state ──────────────────────────────────────────────────

if not entry_section:
    raise SystemExit(
        f"Entry point RVA 0x{entry_rva:x} not in any section!"
    )

eip = (exe.optional_header.image_base + entry_rva) & 0xFFFFFFFF
logger.debug(
    "startup",
    f"Setting EIP = imageBase(0x{exe.optional_header.image_base:x})"
    f" + entryRVA(0x{entry_rva:x}) = 0x{eip:08x}",
)
cpu.eip = eip

# Sentinel HLT so mainCRTStartup return hits a clean halt
SENTINEL_ADDR = 0x001FF000
mem.write8(SENTINEL_ADDR, 0xF4)  # HLT

mem_size = mem.size
stack_base = mem_size - 16
stack_limit = mem_size - (128 * 1024)
cpu.regs[ESP] = stack_base & 0xFFFFFFFF
cpu.regs[EBP] = stack_base & 0xFFFFFFFF

cpu.regs[ESP] -= 4
mem.write32(cpu.regs[ESP], SENTINEL_ADDR)

kernel_structures.initialize_kernel_structures(stack_base, stack_limit, crt_state.process_heap)

# Run the collected DllMain(DLL_PROCESS_ATTACH) calls now: they need a real
# ESP and TEB/PEB, and must finish before the entry point runs.
_failed_static_dllmains = [
    _pending_dll.name for _pending_dll in _pending_dllmain_dlls
    if not _invoke_dependency_dllmain(cpu, mem, crt_state, _pending_dll, _dll_loader_ref, win32_handlers)
]
if _failed_static_dllmains:
    logger.warn(
        "startup",
        f"DllMain(DLL_PROCESS_ATTACH) returned FALSE for: {', '.join(_failed_static_dllmains)} "
        "-- real LoadLibrary would treat this as a failed load; continuing anyway "
        "since these are direct EXE imports we can't simply not-load, but their exports "
        "may be unreliable from here on.",
    )

# ── Build valid EIP range table ───────────────────────────────────────────────

valid_ranges: list[tuple[int, int, str]] = []

for section in exe.section_headers:
    start = exe.optional_header.image_base + section.virtual_address
    end = start + section.virtual_size
    valid_ranges.append((start, end, f"exe:{section.name}"))

logger.debug("startup", "=== DLL Address Mappings ===")
for mapping in exe.import_resolver.get_address_mappings():
    valid_ranges.append((mapping["base_address"], mapping["end_address"], f"dll:{mapping['dll_name']}"))
    logger.debug(
        "startup",
        f"  0x{mapping['base_address']:08x}-0x{mapping['end_address']:08x} {mapping['dll_name']}",
    )

# MAX_HANDLERS (4096) × HANDLER_SIZE (32) = 0x20000 bytes from HANDLER_BASE
valid_ranges.append((0x00200000, 0x00220000, "stubs"))
valid_ranges.append((SENTINEL_ADDR, SENTINEL_ADDR + 1, "sentinel-hlt"))
valid_ranges.append((0x001FE000, 0x001FE004, "thread-sentinel"))
valid_ranges.append((0x08000000, 0x09000000, "thread-stacks"))


# O(1) EIP validity check: map 4KB page numbers to region names.
# Built once from valid_ranges; dynamically-loaded DLLs fall through to
# is_in_dll_range which handles them via the import resolver.
_eip_page_to_name: dict[int, str] = {}
for _vr_start, _vr_end, _vr_name in valid_ranges:
    for _page in range(_vr_start >> 12, (_vr_end + 0xFFF) >> 12):
        _eip_page_to_name[_page] = _vr_name


def is_valid_eip(eip: int) -> str | None:
    name = _eip_page_to_name.get(eip >> 12)
    if name:
        return name
    if exe.import_resolver.is_in_dll_range(eip):
        return "dll:dynamic"
    return None


# ── Debugger: breakpoints and logpoints ──────────────────────────────────────
# Breakpoints halt before the instruction and call handler(cpu, mem); resume is
# automatic. Logpoints call fn(eip, regs, memory, memory_size) inline from the
# Zig loop without halting. Both tables hold 8 entries and silently drop extra
# registrations (TODO.md), so keep each at <= 8.

_bp_handlers: dict = {}   # eip -> callable(cpu, mem)

def register_breakpoint(eip: int, handler) -> None:
    _bp_handlers[eip] = handler
    cpu.add_breakpoint(eip)

def unregister_breakpoint(eip: int) -> None:
    _bp_handlers.pop(eip, None)
    cpu.remove_breakpoint(eip)

def _dispatch_breakpoint() -> None:
    if not cpu.breakpoint_hit:
        return
    hit_eip = cpu.breakpoint_hit_eip
    cpu.clear_breakpoint_hit()            # unhalt + clear flag
    h = _bp_handlers.get(hit_eip)
    keep = True
    if h:
        result = h(cpu, mem)
        if result is False:               # handler returns False → one-shot, remove
            keep = False
    # Execute the halted instruction once without re-triggering the breakpoint.
    cpu.remove_breakpoint(hit_eip)
    cpu.run(1)
    if keep and hit_eip in _bp_handlers:
        cpu.add_breakpoint(hit_eip)


# Execution-history capture to ClickHouse (cpu/src/history/capture.zig): off by
# default. It hooks every write and register change, so only enable it for a
# narrow step window (set _HISTORY_CAPTURE_DONE = False and the window below).
_HISTORY_CAPTURE_ENABLED = False
_HISTORY_CAPTURE_DONE = True
_HISTORY_CAPTURE_START_STEP = 0
_HISTORY_CAPTURE_STOP_STEP = 9_000_000

logger.info("startup", "=== Starting Emulation ===")

# TEW_PROFILE=<file>: cProfile the host. Stats are dumped explicitly before the
# final os._exit(), which skips normal atexit handling.
import cProfile as _cProfile

_TEW_PROFILE = os.environ.get("TEW_PROFILE")
_profiler = _cProfile.Profile() if _TEW_PROFILE else None
if _profiler is not None:
    _profiler.enable()

# Guest step cap for the whole run: unlimited unless TEW_MAX_STEPS is set.
_max_steps_env = os.environ.get("TEW_MAX_STEPS", "").strip()
MAX_STEPS: int | None = int(_max_steps_env) if _max_steps_env else None


def _steps_left(n: int, step_count: int) -> int:
    """n, clipped to what TEW_MAX_STEPS still allows (n itself when unlimited)."""
    return n if MAX_STEPS is None else min(n, MAX_STEPS - step_count)


# TEW_WATCH_ADDR=<hex>: report the last write to that address at halt. Pair
# with TEW_FIXED_HEARTBEAT_MS so heap addresses repeat between runs.
_TEW_WATCH_ADDR = os.environ.get("TEW_WATCH_ADDR")
_tew_watch_addr_int = None
if _TEW_WATCH_ADDR is not None:
    _tew_watch_addr_int = int(_TEW_WATCH_ADDR, 16)
    cpu.set_watchpoint(_tew_watch_addr_int)


# Automation events (tew/automation): game-state facts read out of guest memory.
# gui_begin / gui_exit fire when GUI::OnBegin / GUI::OnExit are entered. The log
# skips the noisy widget classes (tew.automation.gui_events.LOG_SKIP_CLASSES);
# other subscribers see everything.
automation = AutomationEventEmitter()
# TEW_LOG_ALL_GUI=1 logs every GUI event, including the LOG_SKIP_CLASSES widgets.
automation.subscribe(EventLogger(skip=None if os.environ.get("TEW_LOG_ALL_GUI") else skip_noisy_classes))
install_gui_events(cpu, automation)
# Steps per batch (also the virtual-clock tick interval).
# _TIMER_waitticks spins without Sleep/SleepEx so multimedia timers never fire
# from the normal SleepEx path.  Advancing the clock here lets due callbacks fire.
_TIMER_HEARTBEAT_INTERVAL = 100_000
# Cap on wall-clock ms credited to the virtual clock per heartbeat, so a pause
# (debugger, suspend) can't fire a backlog of timers at once.
_TIMER_HEARTBEAT_MAX_MS = 5_000
# TEW_FIXED_HEARTBEAT_MS=<ms>: credit a fixed time per heartbeat instead of
# real elapsed time, so thread scheduling (and DLL bases, heap layout) repeat
# run to run.
_TEW_FIXED_HEARTBEAT_MS = os.environ.get("TEW_FIXED_HEARTBEAT_MS")
if _TEW_FIXED_HEARTBEAT_MS is not None:
    _TEW_FIXED_HEARTBEAT_MS = int(_TEW_FIXED_HEARTBEAT_MS)

step_count = 0
last_valid_step = 0
last_valid_eip = 0
last_valid_region = ""
detected_runaway = False

# Resolved once on first heartbeat call.
_pending_timers = None
_invoke_emulated_proc_fn = None
_get_dialog_sentinel_fn = None
_time_callback_event_set = None
_event_handle_cls = None
_heartbeat_count = 0


def _run_timer_heartbeat() -> None:
    global _heartbeat_count
    global _pending_timers, _invoke_emulated_proc_fn, _get_dialog_sentinel_fn
    global _time_callback_event_set, _event_handle_cls
    global _last_heartbeat_wall_time
    _heartbeat_count += 1
    if _pending_timers is None:
        from tew.api._state import EventHandle as _eh
        from tew.api.user32_handlers import _get_dialog_sentinel as _gds
        from tew.api.user32_handlers import _invoke_emulated_proc as _iep
        from tew.api.win32_handlers import _TIME_CALLBACK_EVENT_SET as _tces
        from tew.api.win32_handlers import pending_timers as _pt
        _pending_timers = _pt
        _invoke_emulated_proc_fn = _iep
        _get_dialog_sentinel_fn = _gds
        _time_callback_event_set = _tces
        _event_handle_cls = _eh
    if _TEW_FIXED_HEARTBEAT_MS is not None:
        elapsed_ms = _TEW_FIXED_HEARTBEAT_MS
    else:
        now = time.monotonic()
        elapsed_ms = int((now - _last_heartbeat_wall_time) * 1000)
        elapsed_ms = max(1, min(elapsed_ms, _TIMER_HEARTBEAT_MAX_MS))
        _last_heartbeat_wall_time = now
    crt_state.scheduler.tick(elapsed_ms, mem)
    if not _pending_timers:
        return
    due = [t for t in list(_pending_timers.values()) if t.due_at <= crt_state.virtual_ticks_ms]
    if not due:
        return
    sentinel = _get_dialog_sentinel_fn(crt_state, mem)
    for timer in due:
        if timer.fu_event & _time_callback_event_set:
            obj = crt_state.kernel_handle_map.get(timer.cb_addr)
            if isinstance(obj, _event_handle_cls):
                obj.signaled = True
                crt_state.scheduler.unblock_handle(timer.cb_addr)
        elif timer.cb_addr != 0:
            _invoke_emulated_proc_fn(cpu, mem, timer.cb_addr, [timer.id, 0, timer.dw_user, 0, 0], sentinel)
        if timer.period_ms > 0:
            timer.due_at += timer.period_ms
        else:
            _pending_timers.pop(timer.id, None)


_heartbeat_countdown = _TIMER_HEARTBEAT_INTERVAL
# Captured here (not earlier) so DLL loading / breakpoint setup above isn't
# credited as elapsed emulation time for the first heartbeat.
_last_heartbeat_wall_time = time.monotonic()
_sample_countdown = 1_000_000
_progress_countdown = 5_000_000




                                 # up-to-100k-step cpu.run() batch, not per instruction) -- confirmed
                                 # 2026-09-14 by reading the call site, so total calls over a whole
                                 # run is only in the tens of thousands; no sampling needed

try:
    while not cpu.halted and (MAX_STEPS is None or step_count < MAX_STEPS) and not detected_runaway:
        if not _HISTORY_CAPTURE_ENABLED and not _HISTORY_CAPTURE_DONE and step_count >= _HISTORY_CAPTURE_START_STEP:
            cpu.enable_history_capture_clickhouse("http://localhost:8123", "default", "poc")
            _HISTORY_CAPTURE_ENABLED = True
            logger.always(WARN, "startup", f"[history] ClickHouse capture enabled at step {step_count:,}, run_id={cpu.run_id}")
        elif _HISTORY_CAPTURE_ENABLED and step_count >= _HISTORY_CAPTURE_STOP_STEP:
            cpu.flush_history_capture()
            cpu.disable_history_capture()
            _HISTORY_CAPTURE_ENABLED = False
            _HISTORY_CAPTURE_DONE = True
            logger.always(WARN, "startup", f"[history] ClickHouse capture disabled at step {step_count:,} (window closed)")
        if os.path.exists(_TEW_PAUSE_TRIGGER):
            try:
                with open(_TEW_PAUSE_TRIGGER, "r") as _f:
                    _pause_cmd = _f.readline().strip().lower()
                os.remove(_TEW_PAUSE_TRIGGER)
            except OSError as _e:
                logger.error("startup", f"[pause-trigger] failed to read/consume trigger: {_e}")
                _pause_cmd = ""
            if _pause_cmd == "pause":
                _tew_paused = True
                logger.always(WARN, "startup", f"[pause] paused at step={step_count:,} EIP=0x{cpu.eip & 0xFFFFFFFF:08x}")
            elif _pause_cmd == "resume":
                _tew_paused = False
                logger.always(WARN, "startup", f"[pause] resumed at step={step_count:,}")
            else:
                logger.error("startup", f"[pause-trigger] unrecognized command: {_pause_cmd!r} (expected pause/resume)")

        if _tew_paused and os.path.exists(_TEW_STEP_TRIGGER):
            try:
                with open(_TEW_STEP_TRIGGER, "r") as _f:
                    _step_line = _f.readline().strip()
                os.remove(_TEW_STEP_TRIGGER)
            except OSError as _e:
                logger.error("startup", f"[step-trigger] failed to read/consume trigger: {_e}")
                _step_line = ""
            try:
                _step_n = int(_step_line) if _step_line else 1
            except ValueError:
                _step_n = 0
                logger.error("startup", f"[step-trigger] unparseable step count: {_step_line!r}")
            if _step_n > 0:
                _step_n = _steps_left(_step_n, step_count)
                cpu.run(_step_n)
                step_count += _step_n
                logger.always(WARN, "startup",
                    f"[step] advanced {_step_n} step(s) -> step={step_count:,} EIP=0x{cpu.eip & 0xFFFFFFFF:08x}")

        eip_before = cpu.eip
        if _tew_paused:
            time.sleep(0.05)  # idle-wait for a resume/step trigger, don't busy-spin a core
        else:
            batch = _steps_left(_TIMER_HEARTBEAT_INTERVAL, step_count)
            cpu.run(batch)
            step_count += batch


        if (_TEW_CLOSE_AFTER_SEC and not _close_injected
                and time.monotonic() - _click_start_wall_time >= float(_TEW_CLOSE_AFTER_SEC)):
            _inject_window_close()
            _close_injected = True

        if _click_file_trigger is not None and not _click_file_trigger.fired and _click_file_trigger.poll():
            if _auto_click_xy is not None:
                import tew.api.d3d8._state as _d3d8_state
                _TEW_CLICK_AT = "%d,%d" % crt_state.window_manager.to_physical_xy(
                    _d3d8_state._vk_hwnd, *_auto_click_xy)
            _TEW_CLICK_AFTER_SEC = str(
                time.monotonic() - _click_start_wall_time + _TEW_CLICK_WHEN_DELAY_SEC)
            logger.error("startup",
                f"[click-trigger] {_TEW_CLICK_WHEN_TEXT!r} appeared in {_TEW_CLICK_WHEN_FILE} -- "
                f"clicking at {_TEW_CLICK_AT} in {_TEW_CLICK_WHEN_DELAY_SEC}s")

        _fist_n = cpu.fist_invalid_count
        if _fist_n != _fist_seen:
            if _fist_n <= 20 or _fist_n % 1000 == 0:
                logger.error("cpu",
                    f"[fist-invalid] out-of-range FIST/FISTP store(s): total={_fist_n}, "
                    f"last at EIP=0x{cpu.fist_invalid_eip:08x}, source={cpu.fist_invalid_val!r}, "
                    f"callers={['0x%08x' % r for r in cpu.fist_invalid_callers]} "
                    f"(guest was given the integer indefinite)")
            _fist_seen = _fist_n

        if (_TEW_CLICK_AT and _TEW_CLICK_AFTER_SEC and not _click_premove_injected
                and time.monotonic() - _click_start_wall_time
                >= float(_TEW_CLICK_AFTER_SEC) - _TEW_CLICK_PREMOVE_SEC):
            # Move onto the target first; a button-down with no prior hover is ignored.
            _inject_mouse_motion(*(int(v) for v in _TEW_CLICK_AT.split(",")))
            _click_premove_injected = True

        if (_TEW_CLICK_AT and _TEW_CLICK_AFTER_SEC and not _click_down_injected
                and time.monotonic() - _click_start_wall_time >= float(_TEW_CLICK_AFTER_SEC)):
            _rel_x, _rel_y = (int(v) for v in _TEW_CLICK_AT.split(","))
            _inject_click_down(_rel_x, _rel_y)
            _click_down_injected = True
            _click_down_wall_time = time.monotonic()
        elif (_click_down_injected and not _click_up_injected
                and time.monotonic() - _click_down_wall_time >= _TEW_CLICK_HOLD_SEC):
            _rel_x, _rel_y = (int(v) for v in _TEW_CLICK_AT.split(","))
            _inject_click_up(_rel_x, _rel_y)
            _click_up_injected = True
            if _auto_click_steps:
                # Chain to the next baked-in click.
                _TEW_CLICK_AT = None
                _TEW_CLICK_AFTER_SEC = None
                _click_premove_injected = _click_down_injected = _click_up_injected = False
                if _arm_next_auto_click():
                    import tew.api.d3d8._state as _d3d8_state
                    _TEW_CLICK_AT = "%d,%d" % crt_state.window_manager.to_physical_xy(
                        _d3d8_state._vk_hwnd, *_auto_click_xy)
                    _TEW_CLICK_AFTER_SEC = str(
                        time.monotonic() - _click_start_wall_time + _TEW_CLICK_WHEN_DELAY_SEC)
                    logger.error("startup",
                        f"[click-trigger] next baked-in click at {_TEW_CLICK_AT} in "
                        f"{_TEW_CLICK_WHEN_DELAY_SEC}s (fixed delay after the previous click)")

        if os.path.exists(_TEW_LOG_TRIGGER):
            try:
                with open(_TEW_LOG_TRIGGER, "r") as _f:
                    _log_trigger_line = _f.readline().strip()
                os.remove(_TEW_LOG_TRIGGER)
            except OSError as _e:
                logger.error("startup", f"[log-trigger] failed to read/consume trigger: {_e}")
                _log_trigger_line = ""
            _log_parts = _log_trigger_line.split(",", 1)
            _new_level = _log_parts[0].strip() or None
            _new_categories = _log_parts[1].strip() if len(_log_parts) > 1 else None
            _new_categories = _new_categories if _new_categories else None
            if _new_level or _new_categories:
                configure_logger(level=_new_level, categories=_new_categories)
                logger.always(WARN, "startup",
                    f"[log-trigger] level={_new_level!r} categories={_new_categories!r}")
            else:
                logger.error("startup",
                    f"[log-trigger] trigger file had unparseable content: {_log_trigger_line!r}")

        if _manual_click_step == 0 and os.path.exists(_TEW_MANUAL_CLICK_TRIGGER):
            try:
                with open(_TEW_MANUAL_CLICK_TRIGGER, "r") as _f:
                    _trigger_line = _f.readline().strip()
                os.remove(_TEW_MANUAL_CLICK_TRIGGER)
            except OSError as _e:
                logger.error("startup", f"[click] failed to read/consume manual-click trigger: {_e}")
                _trigger_line = ""
            _trigger_parts = [p.strip() for p in _trigger_line.split(",") if p.strip()]
            _trigger_cmd = _trigger_parts[0].upper() if _trigger_parts else ""
            # Manual step-by-step clicks for when the bundled sequence isn't enough (e.g.
            # wiggle until a control highlights, then press):
            #   MOVE,x,y   one MOUSEMOTION
            #   DOWN,x,y   button-down (no auto-release)
            #   UP,x,y     button-up
            if _trigger_cmd in ("MOVE", "DOWN", "UP") and len(_trigger_parts) >= 3:
                _mx, _my = int(_trigger_parts[1]), int(_trigger_parts[2])
                if _trigger_cmd == "MOVE":
                    _inject_mouse_motion(_mx, _my)
                elif _trigger_cmd == "DOWN":
                    _inject_click_down(_mx, _my, hold_sec_for_log=0.0)
                else:
                    _inject_click_up(_mx, _my)
                logger.always(WARN, "startup", f"[manual click] {_trigger_cmd} at ({_mx},{_my})")
            elif len(_trigger_parts) >= 2 and _trigger_cmd not in ("MOVE", "DOWN", "UP"):
                _mx, _my = int(_trigger_parts[0]), int(_trigger_parts[1])
                _manual_click_hold_sec = float(_trigger_parts[2]) if len(_trigger_parts) >= 3 else 0.5
                _manual_click_premove_sec = float(_trigger_parts[3]) if len(_trigger_parts) >= 4 else 0.3
                _manual_click_xy = (_mx, _my)
                _inject_mouse_motion(_mx, _my)
                _manual_click_step = 1
                _manual_click_step_wall_time = time.monotonic()
                logger.always(WARN, "startup",
                    f"[manual click] triggered at ({_mx},{_my}), premove={_manual_click_premove_sec}s "
                    f"hold={_manual_click_hold_sec}s")
            else:
                logger.error("startup",
                    f"[click] manual-click trigger file had unparseable content: {_trigger_line!r}")
        elif (_manual_click_step == 1 and _manual_click_xy is not None
                and time.monotonic() - _manual_click_step_wall_time >= _manual_click_premove_sec):
            _inject_click_down(*_manual_click_xy, hold_sec_for_log=_manual_click_hold_sec)
            _manual_click_step = 2
            _manual_click_step_wall_time = time.monotonic()
        elif (_manual_click_step == 2 and _manual_click_xy is not None
                and time.monotonic() - _manual_click_step_wall_time >= _manual_click_hold_sec):
            _inject_click_up(*_manual_click_xy)
            _manual_click_step = 0
            _manual_click_xy = None

        if (_TEW_DBLCLICK_AT and _TEW_DBLCLICK_AFTER_SEC
                and _dblclick_attempt < _TEW_DBLCLICK_REPEAT):
            _dx, _dy = (int(v) for v in _TEW_DBLCLICK_AT.split(","))
            _elapsed = time.monotonic() - _click_start_wall_time
            _attempt_start = float(_TEW_DBLCLICK_AFTER_SEC) + _dblclick_attempt * _TEW_DBLCLICK_RETRY_SEC
            if _dblclick_step == 0 and _elapsed >= _attempt_start:
                _inject_click_down(_dx, _dy, hold_sec_for_log=_TEW_DBLCLICK_HOLD_SEC)
                _dblclick_step = 1
                _dblclick_step_wall_time = time.monotonic()
                logger.always(WARN, "startup",
                    f"[dblclick] attempt {_dblclick_attempt + 1}/{_TEW_DBLCLICK_REPEAT} click 1 down")
            elif (_dblclick_step == 1
                    and time.monotonic() - _dblclick_step_wall_time >= _TEW_DBLCLICK_HOLD_SEC):
                _inject_click_up(_dx, _dy)
                _dblclick_step = 2
                _dblclick_step_wall_time = time.monotonic()
            elif (_dblclick_step == 2
                    and time.monotonic() - _dblclick_step_wall_time >= _TEW_DBLCLICK_GAP_SEC):
                _inject_click_down(_dx, _dy, hold_sec_for_log=_TEW_DBLCLICK_HOLD_SEC)
                _dblclick_step = 3
                _dblclick_step_wall_time = time.monotonic()
                logger.always(WARN, "startup",
                    f"[dblclick] attempt {_dblclick_attempt + 1}/{_TEW_DBLCLICK_REPEAT} click 2 down")
            elif (_dblclick_step == 3
                    and time.monotonic() - _dblclick_step_wall_time >= _TEW_DBLCLICK_HOLD_SEC):
                _inject_click_up(_dx, _dy)
                _dblclick_step = 0
                _dblclick_attempt += 1

        if cpu.faulted:
            # Let the game's SEH chain handle it first (tew/kernel/seh.py). The core only
            # produces access violations, so that's the exception reported.
            fault_eip = cpu.eip & 0xFFFFFFFF
            if cpu.unknown_opcode:
                # cpu.eip is already past the opcode byte; last_instr_eip is the instruction
                # that failed to decode.
                logger.always(WARN, "seh",
                    f"Unknown opcode: 0x{cpu.last_opcode:02x} at EIP=0x{cpu.last_instr_eip:08x}")
                # A missing opcode is a tew gap, not a guest exception: halt here, no SEH.
                logger.error("seh",
                    f"halting immediately: opcode 0x{cpu.last_opcode:02x} at "
                    f"0x{cpu.last_instr_eip:08x} is not implemented by the CPU core "
                    f"(game SEH chain deliberately NOT run)")
                cpu.faulted = True
                cpu.halted = True
                break
            logger.always(WARN, "seh", f"CPU fault at EIP=0x{fault_eip:08x} -- attempting SEH dispatch")
            handled = dispatch_exception(cpu, mem, STATUS_ACCESS_VIOLATION, fault_eip)
            if handled:
                logger.info("seh", f"fault at 0x{fault_eip:08x} handled by game's own SEH chain -- resuming")
                cpu.faulted = False
            else:
                logger.error("seh", f"fault at 0x{fault_eip:08x} unhandled by SEH chain -- halting")
                try:
                    raw = [f"{mem.read8(fault_eip + i):02x}" for i in range(16)]
                    logger.error("seh", f"  Bytes at fault EIP: {' '.join(raw)}")
                except Exception:
                    logger.error("seh", "  Bytes at fault EIP: (out of bounds)")
                # Break out now. cpu.faulted is not in the loop condition, the native flag
                # clears on the next successful run (including the SEH walk above), and
                # preempt_slice would run other threads on a broken CPU. Setting cpu.faulted
                # makes the sticky flag survive, so the post-run report treats this as a fault.
                cpu.faulted = True
                cpu.halted = True
                break

        if _bp_handlers:
            _dispatch_breakpoint()
        # Report every watchpoint hit, then re-arm; the core keeps only the last write.
        if _tew_watch_addr_int is not None and cpu.watchpoint_hit:
            logger.error("cpu", f"[watchpoint-hit-live] EIP=0x{cpu.watchpoint_eip:08x} written=0x{cpu.watchpoint_val:02x} step={step_count}")
            cpu.set_watchpoint(_tew_watch_addr_int)
            cpu.halted = False
        crt_state.scheduler.preempt_slice(cpu, mem)

        _heartbeat_countdown -= batch
        if _heartbeat_countdown <= 0:
            _heartbeat_countdown = _TIMER_HEARTBEAT_INTERVAL
            _run_timer_heartbeat()

        _sample_countdown -= batch
        if _sample_countdown <= 0:
            _sample_countdown = 1_000_000
            logger.debug(
                "watch",
                f"[EIP sample @ {step_count}] EIP=0x{cpu.eip & 0xFFFFFFFF:08x}"
                f" ESP=0x{cpu.regs[ESP] & 0xFFFFFFFF:08x}",
            )

        _progress_countdown -= batch
        if _progress_countdown <= 0:
            _progress_countdown = 5_000_000
            eip_now = cpu.eip & 0xFFFFFFFF
            stub_note = ""
            if 0x00200000 <= eip_now < 0x00220000:
                recent = win32_handlers.get_call_log()[-8:]
                stub_note = f" calls={recent}"
            logger.debug(
                "startup",
                f"[alive] step={step_count:,} EIP=0x{eip_now:08x}{stub_note}"
                f" vtime={crt_state.virtual_ticks_ms}ms",
            )

        region = is_valid_eip(cpu.eip)
        if region:
            last_valid_step = step_count
            last_valid_eip = eip_before
            last_valid_region = region
        elif not detected_runaway and step_count > 100:
            # EIP left every known code region. Windows would fault on the fetch; tew has no
            # page protection, so raise an access violation through the normal SEH path.
            runaway_eip = cpu.eip & 0xFFFFFFFF
            logger.always(
                WARN,
                "seh",
                f"RUNAWAY at step {step_count}, EIP=0x{runaway_eip:08x} (last valid "
                f"step {last_valid_step}, EIP=0x{last_valid_eip & 0xFFFFFFFF:08x} in "
                f"{last_valid_region}) -- attempting SEH dispatch",
            )
            handled = dispatch_exception(cpu, mem, STATUS_ACCESS_VIOLATION, runaway_eip)
            if handled:
                logger.info("seh", f"runaway at 0x{runaway_eip:08x} handled by game's own SEH chain -- resuming")
            else:
                logger.error("seh", f"runaway at 0x{runaway_eip:08x} unhandled by SEH chain -- halting")
                try:
                    raw = [f"{mem.read8(runaway_eip + i):02x}" for i in range(16)]
                    logger.error("seh", f"  Bytes at EIP: {' '.join(raw)}")
                except Exception:
                    logger.error("seh", "  Bytes at EIP: (out of bounds)")
                detected_runaway = True
                cpu.halted = True
                break
except FatalHaltError as e:
    # Raised by cpu.run()/step() the moment a fatal halt happens anywhere. cpu.halted
    # is set, so the reporting below handles it.
    logger.error("cpu", f"Fatal halt: {e}")

if MAX_STEPS is not None and step_count >= MAX_STEPS:
    logger.warn("cpu", f"Execution limit reached ({MAX_STEPS} steps)")

# ── Post-run reporting ────────────────────────────────────────────────────────

if crt_state.fatal_dialogs:
    logger.error("startup", "=== Emulation Complete (NOT a clean exit) ===")
    logger.error(
        "startup",
        f"{len(crt_state.fatal_dialogs)} fatal (stop/hand-icon) dialog(s) fired"
        " during this run -- a voluntary ExitProcess afterward is the game"
        " aborting, not a successful run:",
    )
    for caption, text in crt_state.fatal_dialogs:
        logger.error("startup", f'  "{caption}": {text.splitlines()[0] if text else ""}')
else:
    logger.info("startup", "=== Emulation Complete (clean exit) ===")
    if cpu.fist_invalid_count:
        logger.error("cpu",
            f"[fist-invalid] TOTAL out-of-range FIST/FISTP stores this run: {cpu.fist_invalid_count}, "
            f"last at EIP=0x{cpu.fist_invalid_eip:08x}, source={cpu.fist_invalid_val!r}")
logger.info("startup", f"Steps executed: {cpu._step_count}")

logger.debug("handlers", "--- Win32 Stub Call Log (last 50) ---")
for call in win32_handlers.get_call_log()[-50:]:
    logger.debug("handlers", f"  {call}")

if cpu.watchpoint_hit:
    logger.error("exception",
        f"WATCHPOINT HIT at EIP=0x{cpu.watchpoint_eip:08x}"
        f"  written=0x{cpu.watchpoint_val:02x}"
        f"  (first byte of write to watchpoint address)")
    diagnose_halt(cpu, exe.import_resolver)
elif cpu.faulted:
    diagnose_fault(cpu, exe.import_resolver, memory=mem, state=crt_state)
elif cpu.halted:
    diagnose_halt(cpu, exe.import_resolver)

logger.info("startup", f"Final EIP: 0x{cpu.eip & 0xFFFFFFFF:08x}")

# Flush history capture (only if it was enabled) and log the run id.
if _HISTORY_CAPTURE_ENABLED:
    history_run_id = cpu.run_id
    cpu.flush_history_capture()
    logger.info("startup", f"[history] run_id={history_run_id} flushed to ClickHouse (history_events table)")

# Tear down SDL2 (and any windows/renderers it owns) before exiting -- an
# implicit process exit with SDL2 still live left the NVIDIA driver's own
# atexit cleanup to run against a live GL/Vulkan-backed context, which
# segfaulted inside the driver itself (libnvidia-rtcore.so), not our code.
crt_state.window_manager.shutdown()

# os._exit(), not sys.exit(): NVIDIA's atexit handlers crash after SDL_Quit has
# closed the X connection. The logger flushes every line, so nothing is lost.
sys.stdout.flush()

if _profiler is not None:
    _profiler.disable()
    _profiler.dump_stats(_TEW_PROFILE)
    logger.info("startup", f"[profile] cProfile stats dumped to {_TEW_PROFILE}")

os._exit(1 if cpu.faulted else 0)
