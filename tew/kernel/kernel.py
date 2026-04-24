"""Kernel — async I/O completion source.

Tracks pending async socket operations registered via WSAAsyncSelect or
WSAEventSelect.  Called from the scheduler when no thread is READY so that
completed I/O can unblock waiting threads without requiring a heartbeat race.

Real Windows signals event handles from kernel/driver completions without
any user thread running.  We approximate that here: when the scheduler finds
no ready thread it calls Kernel.tick(), which polls real Python sockets and
delivers completions.
"""

from __future__ import annotations

import select as _sel
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.api._state import CRTState

from tew.logger import logger

# WinSock FD_* event bits (matches <winsock2.h>)
FD_READ    = 0x01
FD_WRITE   = 0x02
FD_OOB     = 0x04
FD_ACCEPT  = 0x08
FD_CONNECT = 0x10
FD_CLOSE   = 0x20


@dataclass
class _AsyncSelectReg:
    hwnd:   int
    msg:    int
    events: int


@dataclass
class _EventSelectReg:
    event_handle: int
    events:       int


class Kernel:
    """Async I/O completion source wired into the cooperative scheduler.

    Socket stubs register interest here.  The scheduler calls tick() when
    no thread is READY.  tick() polls real Python sockets with a zero-timeout
    select(), then for each ready socket:

    * WSAAsyncSelect registrations → post a window message to hwnd so the
      game's message loop can dispatch it (window proc may call SetEvent).
    * WSAEventSelect registrations → signal the event handle directly and
      call scheduler.unblock_handle() so blocked threads become READY.
    """

    def __init__(self, state: "CRTState") -> None:
        self._state = state
        self._async_select: dict[int, _AsyncSelectReg]  = {}
        self._event_select: dict[int, _EventSelectReg]  = {}
        self._connect_fired: set[int] = set()   # sockets whose FD_CONNECT was delivered

    # ── Registration ──────────────────────────────────────────────────────────

    def register_async_select(self, socket: int, hwnd: int,
                               msg: int, events: int) -> None:
        """WSAAsyncSelect: post msg to hwnd when socket is ready for events."""
        if events == 0:
            self._async_select.pop(socket, None)
        else:
            self._async_select[socket] = _AsyncSelectReg(
                hwnd=hwnd, msg=msg, events=events)
        logger.debug("kernel",
            f"register_async_select: socket=0x{socket:x} hwnd=0x{hwnd:x} "
            f"msg=0x{msg:x} events=0x{events:x}")

    def register_event_select(self, socket: int,
                               event_handle: int, events: int) -> None:
        """WSAEventSelect: signal event_handle when socket is ready for events."""
        if events == 0:
            self._event_select.pop(socket, None)
        else:
            self._event_select[socket] = _EventSelectReg(
                event_handle=event_handle, events=events)
        logger.debug("kernel",
            f"register_event_select: socket=0x{socket:x} "
            f"event=0x{event_handle:x} events=0x{events:x}")

    def unregister_socket(self, socket: int) -> None:
        """Remove all registrations for a socket (called on closesocket)."""
        self._async_select.pop(socket, None)
        self._event_select.pop(socket, None)
        self._connect_fired.discard(socket)

    # ── Tick ─────────────────────────────────────────────────────────────────

    def tick(self) -> None:
        """Poll registered sockets; deliver completions for any that are ready.

        Called from Scheduler._pick_next_ready() when no thread is READY.
        A zero-timeout select() is used so this never blocks.
        """
        from tew.api.wsock32_handlers import _socket_map  # lazy — avoids import cycle

        all_sockets = set(self._async_select) | set(self._event_select)
        if not all_sockets:
            return

        rd_pairs: list[tuple[int, object]] = []
        wr_pairs: list[tuple[int, object]] = []

        for sh in all_sockets:
            entry = _socket_map.get(sh)
            if entry is None or entry.py_sock is None:
                continue
            mask = 0
            if sh in self._async_select:
                mask |= self._async_select[sh].events
            if sh in self._event_select:
                mask |= self._event_select[sh].events
            if mask & (FD_READ | FD_CLOSE):
                rd_pairs.append((sh, entry.py_sock))
            if mask & (FD_CONNECT | FD_WRITE):
                wr_pairs.append((sh, entry.py_sock))

        if not rd_pairs and not wr_pairs:
            return

        try:
            ready_r, ready_w, _ = _sel.select(
                [s for _, s in rd_pairs],
                [s for _, s in wr_pairs],
                [],
                0.0,
            )
        except OSError:
            return

        for sh, py_sock in rd_pairs:
            if py_sock in ready_r:
                self._deliver(sh, FD_READ)

        for sh, py_sock in wr_pairs:
            if py_sock in ready_w:
                reg_mask = 0
                if sh in self._async_select:
                    reg_mask |= self._async_select[sh].events
                if sh in self._event_select:
                    reg_mask |= self._event_select[sh].events
                if (reg_mask & FD_CONNECT) and sh not in self._connect_fired:
                    self._deliver(sh, FD_CONNECT)
                    self._connect_fired.add(sh)
                elif reg_mask & FD_WRITE:
                    self._deliver(sh, FD_WRITE)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _deliver(self, socket: int, event_bit: int) -> None:
        """Deliver one network event notification for socket."""
        from tew.api._state import EventHandle

        # lParam layout for WSAAsyncSelect messages:
        #   low word  = event code, high word = error code (0 = no error)
        lparam = struct.unpack("<I", struct.pack("<HH", event_bit, 0))[0]

        reg = self._async_select.get(socket)
        if reg is not None and (reg.events & event_bit):
            ok = self._state.window_manager.post_message(
                reg.hwnd, reg.msg, socket, lparam)
            logger.info("kernel",
                f"kernel.tick: socket=0x{socket:x} event={event_bit:#x} "
                f"-> PostMessage(hwnd=0x{reg.hwnd:x} msg=0x{reg.msg:x}) ok={ok}")

        ereg = self._event_select.get(socket)
        if ereg is not None and (ereg.events & event_bit):
            obj = self._state.kernel_handle_map.get(ereg.event_handle)
            if isinstance(obj, EventHandle):
                obj.signaled = True
                self._state.scheduler.unblock_handle(ereg.event_handle)
                logger.info("kernel",
                    f"kernel.tick: socket=0x{socket:x} event={event_bit:#x} "
                    f"-> SetEvent(handle=0x{ereg.event_handle:x})")
