"""wsock32.dll / ws2_32.dll handler implementations for the x86 emulator.

MCO uses WinSock for both HTTP (via WinINet) and raw TCP/UDP socket I/O to
the NPS lobby and game servers.  This module forwards raw socket calls to
real Python sockets so the game can communicate with a running server.

Each Win32 socket handle maps to a ``SocketEntry`` that holds the Python
``socket.socket`` object and per-socket metadata (address family, type,
non-blocking flag).

All functions exported by ordinal are registered as "Ordinal #N" to match
the name the import resolver derives from the IAT.

Calling conventions (all stdcall):
  WSAStartup(WORD, LPWSADATA)               — 2 args, 8 bytes
  WSACleanup()                              — 0 args, 0 bytes
  WSAGetLastError()                         — 0 args, 0 bytes
  WSASetLastError(int)                      — 1 arg,  4 bytes
  socket(af, type, protocol)                — 3 args, 12 bytes
  closesocket(s)                            — 1 arg,  4 bytes
  connect(s, addr, namelen)                 — 3 args, 12 bytes
  send(s, buf, len, flags)                  — 4 args, 16 bytes
  recv(s, buf, len, flags)                  — 4 args, 16 bytes
  bind(s, addr, namelen)                    — 3 args, 12 bytes
  listen(s, backlog)                        — 2 args, 8 bytes
  select(nfds, read, write, except, time)   — 5 args, 20 bytes
  setsockopt(s, level, optname, val, len)   — 5 args, 20 bytes
  getsockopt(s, level, optname, val, len)   — 5 args, 20 bytes
  ioctlsocket(s, cmd, argp)                 — 3 args, 12 bytes
  WSAAsyncSelect(s, hwnd, msg, events)      — 4 args, 16 bytes
  htonl/htons/ntohl/ntohs                   — 1 arg,  4 bytes
"""

from __future__ import annotations

import select as _select_module
import socket as _socket_module
import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall
from tew.api._state import CRTState, read_cstring
from tew.logger import logger


# ── WinSock constants ─────────────────────────────────────────────────────────

AF_INET          = 2
SOCK_STREAM      = 1
SOCK_DGRAM       = 2

SOCKET_ERROR     = 0xFFFFFFFF   # -1 as DWORD
INVALID_SOCKET   = 0xFFFFFFFF

WSAEWOULDBLOCK   = 10035
WSAENOTSOCK      = 10038
WSAEADDRINUSE    = 10048
WSAECONNREFUSED  = 10061
WSAHOST_NOT_FOUND = 11001

# ioctlsocket command for non-blocking mode
FIONBIO = 0x8004667E

# ── WSADATA layout (WinSock 2.2, 400 bytes) ──────────────────────────────────

_WSADATA_SIZE     = 400
_WSADATA_VERSION  = 0x0202
_WSADATA_HIGHVER  = 0x0202
_WSADATA_DESC     = b"WinSock 2.0\x00"
_WSADATA_STATUS   = b"Running\x00"
_WSADATA_MAXSOCKS = 512
_WSADATA_MAXUDP   = 512


# ── Per-socket state ──────────────────────────────────────────────────────────

@dataclass
class SocketEntry:
    """Tracks one Win32 socket handle and its underlying Python socket."""

    af: int                              # address family (AF_INET = 2)
    type_: int                           # SOCK_STREAM or SOCK_DGRAM
    proto: int                           # protocol number
    py_sock: _socket_module.socket | None = None
    nonblocking: bool = False
    connected_to: str = ""               # "host:port" for logging


# ── Module-level socket table ─────────────────────────────────────────────────

_next_handle: int = 0x100
_socket_map: dict[int, SocketEntry] = {}
_wsa_last_error: int = 0


def _alloc_socket(af: int, type_: int, proto: int) -> int:
    """Allocate a new socket handle and return it."""
    global _next_handle
    h = _next_handle
    _next_handle += 1
    _socket_map[h] = SocketEntry(af=af, type_=type_, proto=proto)
    return h


# ── sockaddr_in helpers ───────────────────────────────────────────────────────

def _read_sockaddr_in(ptr: int, memory: "Memory") -> tuple[str, int]:
    """Parse a sockaddr_in struct from emulator memory.

    Returns (ip_string, port_number) in host byte order.
    The sin_port and sin_addr fields are stored in network byte order in the
    struct, so we byte-swap them on the way out.
    """
    # sin_port is 2 bytes at offset 2, stored big-endian in the struct.
    # memory.read16 interprets bytes as little-endian, so we swap.
    raw_port = memory.read16((ptr + 2) & 0xFFFFFFFF)
    port = struct.unpack(">H", struct.pack("<H", raw_port))[0]

    # sin_addr is 4 bytes at offset 4, stored big-endian (network order).
    # memory.read32 interprets bytes as little-endian, so pack "<I" recreates
    # the original big-endian byte sequence for inet_ntoa.
    raw_addr = memory.read32((ptr + 4) & 0xFFFFFFFF)
    ip = _socket_module.inet_ntoa(struct.pack("<I", raw_addr))

    return ip, port


# ── fd_set helpers ────────────────────────────────────────────────────────────

def _read_fd_set(ptr: int, memory: "Memory") -> list[int]:
    """Read a Win32 fd_set struct and return the list of socket handles.

    fd_set layout:
      [0]   DWORD fd_count
      [4..] SOCKET fd_array[FD_SETSIZE=64]
    """
    if not ptr:
        return []
    count = memory.read32(ptr & 0xFFFFFFFF)
    handles = []
    for i in range(min(count, 64)):
        h = memory.read32((ptr + 4 + i * 4) & 0xFFFFFFFF)
        handles.append(h)
    return handles


def _write_fd_set(ptr: int, handles: list[int], memory: "Memory") -> None:
    """Write a list of socket handles back into a Win32 fd_set struct."""
    if not ptr:
        return
    memory.write32(ptr, len(handles))
    for i, h in enumerate(handles):
        memory.write32((ptr + 4 + i * 4) & 0xFFFFFFFF, h)


# ── register_wsock32_handlers ─────────────────────────────────────────────────

def register_wsock32_handlers(
    stubs: Win32Handlers,
    memory: "Memory",
    state: CRTState,
) -> None:
    """Register all wsock32.dll (and ws2_32.dll) socket API handlers."""

    global _wsa_last_error

    # Allocate a static buffer for gethostbyname results.
    # Layout (96 bytes total):
    #   +0   HOSTENT struct (16 bytes)
    #   +16  h_name string (64 bytes)
    #   +80  h_aliases array (4 bytes: one NULL pointer = empty list)
    #   +84  h_addr_list array (8 bytes: one addr pointer + NULL)
    #   +92  IP address bytes (4 bytes)
    _hostent_buf = state.simple_alloc(96)

    def _reg(name: str, fn, dll: str = "wsock32.dll") -> None:
        stubs.register_handler(dll, name, fn)

    # Ordinal table (WinXP wsock32.dll):
    ordinal_map: dict[int, str] = {
        1:   "accept",           2:   "bind",              3:   "closesocket",
        4:   "connect",          5:   "getpeername",        6:   "getsockname",
        7:   "getsockopt",       8:   "htonl",              9:   "htons",
        10:  "inet_addr",        11:  "inet_ntoa",          12:  "ioctlsocket",
        13:  "listen",           14:  "ntohl",              15:  "ntohs",
        16:  "recv",             17:  "recvfrom",           18:  "select",
        19:  "send",             20:  "sendto",             21:  "setsockopt",
        22:  "shutdown",         23:  "socket",
        101: "WSAAsyncSelect",   102: "WSAAsyncGetHostByAddr",
        103: "WSAAsyncGetHostByName",
        108: "WSACancelAsyncRequest",
        111: "WSAGetLastError",  112: "WSASetLastError",
        113: "WSACancelBlockingCall", 114: "WSAIsBlocking",
        115: "WSAStartup",       116: "WSACleanup",
        151: "__WSAFDIsSet",
    }

    # ── WSA startup / teardown ────────────────────────────────────────────────

    def _wsa_startup(cpu: "CPU") -> None:
        """WSAStartup(WORD wVersionRequested, LPWSADATA lpWSAData) -> int."""
        global _wsa_last_error
        lp_wsa_data = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if lp_wsa_data:
            for i in range(_WSADATA_SIZE):
                memory.write8((lp_wsa_data + i) & 0xFFFFFFFF, 0)
            memory.write16(lp_wsa_data,       _WSADATA_VERSION)
            memory.write16(lp_wsa_data + 2,   _WSADATA_HIGHVER)
            for i, b in enumerate(_WSADATA_DESC):
                memory.write8((lp_wsa_data + 4 + i) & 0xFFFFFFFF, b)
            for i, b in enumerate(_WSADATA_STATUS):
                memory.write8((lp_wsa_data + 261 + i) & 0xFFFFFFFF, b)
            memory.write16(lp_wsa_data + 390, _WSADATA_MAXSOCKS)
            memory.write16(lp_wsa_data + 392, _WSADATA_MAXUDP)
        _wsa_last_error = 0
        logger.debug("handlers", "WSAStartup -> 0 (success)")
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    def _wsa_cleanup(cpu: "CPU") -> None:
        """WSACleanup() -> int."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 0)

    def _wsa_get_last_error(cpu: "CPU") -> None:
        """WSAGetLastError() -> int."""
        cpu.regs[EAX] = _wsa_last_error
        cleanup_stdcall(cpu, memory, 0)

    def _wsa_set_last_error(cpu: "CPU") -> None:
        """WSASetLastError(int iError) -> void."""
        global _wsa_last_error
        _wsa_last_error = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cleanup_stdcall(cpu, memory, 4)

    def _wsa_cancel_blocking_call(cpu: "CPU") -> None:
        """WSACancelBlockingCall() -> int."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 0)

    def _wsa_is_blocking(cpu: "CPU") -> None:
        """WSAIsBlocking() -> BOOL.  Always FALSE — no blocking calls in emulator."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 0)

    def _wsa_async_select(cpu: "CPU") -> None:
        """WSAAsyncSelect(s, hWnd, wMsg, lEvent) -> int.  Not needed."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 16)

    def _wsa_async_get_host_by_addr(cpu: "CPU") -> None:
        """WSAAsyncGetHostByAddr(...) -> HANDLE.  Not supported."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 24)

    def _wsa_async_get_host_by_name(cpu: "CPU") -> None:
        """WSAAsyncGetHostByName(hWnd, wMsg, name, buf, buflen) -> HANDLE."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 20)

    def _wsa_cancel_async_request(cpu: "CPU") -> None:
        """WSACancelAsyncRequest(hAsyncTaskHandle) -> int."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    def _wsa_fd_is_set(cpu: "CPU") -> None:
        """__WSAFDIsSet(SOCKET fd, fd_set* set) -> int."""
        fd  = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        ptr = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        handles = _read_fd_set(ptr, memory)
        cpu.regs[EAX] = 1 if fd in handles else 0
        cleanup_stdcall(cpu, memory, 8)

    # ── Socket lifecycle ──────────────────────────────────────────────────────

    def _socket(cpu: "CPU") -> None:
        """socket(af, type, protocol) -> SOCKET."""
        af    = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        type_ = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        proto = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        h = _alloc_socket(af, type_, proto)
        logger.debug("handlers", f"socket(af={af}, type={type_}, proto={proto}) -> 0x{h:x}")
        cpu.regs[EAX] = h
        cleanup_stdcall(cpu, memory, 12)

    def _closesocket(cpu: "CPU") -> None:
        """closesocket(s) -> int."""
        s = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        entry = _socket_map.pop(s, None)
        if entry and entry.py_sock:
            try:
                entry.py_sock.close()
            except OSError:
                pass
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    def _connect(cpu: "CPU") -> None:
        """connect(s, sockaddr*, namelen) -> int."""
        global _wsa_last_error
        s       = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        sa_ptr  = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)

        entry = _socket_map.get(s)
        if entry is None:
            _wsa_last_error = WSAENOTSOCK
            cpu.regs[EAX] = SOCKET_ERROR
            cleanup_stdcall(cpu, memory, 12)
            return

        ip, port = _read_sockaddr_in(sa_ptr, memory)
        entry.connected_to = f"{ip}:{port}"
        logger.info("socket", f"connect(0x{s:x}) -> {ip}:{port}")

        # Determine Python socket type
        sock_type = (
            _socket_module.SOCK_STREAM
            if entry.type_ == SOCK_STREAM
            else _socket_module.SOCK_DGRAM
        )

        try:
            py_sock = _socket_module.socket(_socket_module.AF_INET, sock_type)
            if entry.nonblocking:
                py_sock.setblocking(False)
            py_sock.connect((ip, port))
            entry.py_sock = py_sock
            _wsa_last_error = 0
            cpu.regs[EAX] = 0  # success
        except BlockingIOError:
            # Non-blocking connect in progress — game will use select() to wait
            entry.py_sock = py_sock
            _wsa_last_error = WSAEWOULDBLOCK
            cpu.regs[EAX] = SOCKET_ERROR
        except OSError as exc:
            logger.warn("socket", f"connect({ip}:{port}) failed: {exc}")
            _wsa_last_error = WSAECONNREFUSED
            cpu.regs[EAX] = SOCKET_ERROR

        cleanup_stdcall(cpu, memory, 12)

    def _bind(cpu: "CPU") -> None:
        """bind(s, addr, namelen) -> int."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 12)

    def _listen(cpu: "CPU") -> None:
        """listen(s, backlog) -> int."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    def _accept(cpu: "CPU") -> None:
        """accept(s, addr, addrlen) -> SOCKET.  Not supported."""
        global _wsa_last_error
        _wsa_last_error = WSAEWOULDBLOCK
        cpu.regs[EAX] = INVALID_SOCKET
        cleanup_stdcall(cpu, memory, 12)

    def _shutdown(cpu: "CPU") -> None:
        """shutdown(s, how) -> int."""
        s = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        entry = _socket_map.get(s)
        if entry and entry.py_sock:
            try:
                entry.py_sock.shutdown(_socket_module.SHUT_RDWR)
            except OSError:
                pass
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    def _getpeername(cpu: "CPU") -> None:
        """getpeername(s, name, namelen) -> int."""
        cpu.regs[EAX] = SOCKET_ERROR
        cleanup_stdcall(cpu, memory, 12)

    def _getsockname(cpu: "CPU") -> None:
        """getsockname(s, name, namelen) -> int."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 12)

    # ── Data transfer ─────────────────────────────────────────────────────────

    def _send(cpu: "CPU") -> None:
        """send(s, buf, len, flags) -> int (bytes sent, or SOCKET_ERROR)."""
        global _wsa_last_error
        s      = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_buf = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        length = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)

        entry = _socket_map.get(s)
        if entry is None or entry.py_sock is None:
            _wsa_last_error = WSAENOTSOCK
            cpu.regs[EAX] = SOCKET_ERROR
            cleanup_stdcall(cpu, memory, 16)
            return

        data = bytes(memory.read8((lp_buf + i) & 0xFFFFFFFF) for i in range(length))
        logger.debug("socket",
            f"send(0x{s:x} -> {entry.connected_to}, {length} bytes): {data[:64]!r}")
        try:
            sent = entry.py_sock.send(data)
            _wsa_last_error = 0
            cpu.regs[EAX] = sent
        except BlockingIOError:
            _wsa_last_error = WSAEWOULDBLOCK
            cpu.regs[EAX] = SOCKET_ERROR
        except OSError as exc:
            logger.warn("socket", f"send failed: {exc}")
            _wsa_last_error = WSAECONNREFUSED
            cpu.regs[EAX] = SOCKET_ERROR

        cleanup_stdcall(cpu, memory, 16)

    def _recv(cpu: "CPU") -> None:
        """recv(s, buf, len, flags) -> int (bytes received, or SOCKET_ERROR)."""
        global _wsa_last_error
        s      = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        lp_buf = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        length = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)

        entry = _socket_map.get(s)
        if entry is None or entry.py_sock is None:
            _wsa_last_error = WSAENOTSOCK
            cpu.regs[EAX] = SOCKET_ERROR
            cleanup_stdcall(cpu, memory, 16)
            return

        try:
            data = entry.py_sock.recv(length)
            if data:
                for i, b in enumerate(data):
                    memory.write8((lp_buf + i) & 0xFFFFFFFF, b)
                _wsa_last_error = 0
                cpu.regs[EAX] = len(data)
                logger.debug("socket",
                    f"recv(0x{s:x} <- {entry.connected_to}, {len(data)} bytes): "
                    f"{data[:64]!r}")
            else:
                # Graceful close from remote
                cpu.regs[EAX] = 0
        except BlockingIOError:
            _wsa_last_error = WSAEWOULDBLOCK
            cpu.regs[EAX] = SOCKET_ERROR
        except OSError as exc:
            logger.warn("socket", f"recv failed: {exc}")
            _wsa_last_error = WSAECONNREFUSED
            cpu.regs[EAX] = SOCKET_ERROR

        cleanup_stdcall(cpu, memory, 16)

    def _sendto(cpu: "CPU") -> None:
        """sendto(s, buf, len, flags, to, tolen) -> int."""
        global _wsa_last_error
        s      = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_buf = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        length = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        sa_ptr = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)

        entry = _socket_map.get(s)
        if entry is None:
            _wsa_last_error = WSAENOTSOCK
            cpu.regs[EAX] = SOCKET_ERROR
            cleanup_stdcall(cpu, memory, 24)
            return

        ip, port = _read_sockaddr_in(sa_ptr, memory) if sa_ptr else ("", 0)

        if entry.py_sock is None:
            try:
                entry.py_sock = _socket_module.socket(
                    _socket_module.AF_INET, _socket_module.SOCK_DGRAM
                )
                if entry.nonblocking:
                    entry.py_sock.setblocking(False)
            except OSError as exc:
                logger.warn("socket", f"sendto: could not create UDP socket: {exc}")
                _wsa_last_error = WSAECONNREFUSED
                cpu.regs[EAX] = SOCKET_ERROR
                cleanup_stdcall(cpu, memory, 24)
                return

        data = bytes(memory.read8((lp_buf + i) & 0xFFFFFFFF) for i in range(length))
        logger.debug("socket", f"sendto(0x{s:x} -> {ip}:{port}, {length} bytes)")
        try:
            sent = entry.py_sock.sendto(data, (ip, port))
            _wsa_last_error = 0
            cpu.regs[EAX] = sent
        except OSError as exc:
            logger.warn("socket", f"sendto failed: {exc}")
            _wsa_last_error = WSAECONNREFUSED
            cpu.regs[EAX] = SOCKET_ERROR

        cleanup_stdcall(cpu, memory, 24)

    def _recvfrom(cpu: "CPU") -> None:
        """recvfrom(s, buf, len, flags, from, fromlen) -> int."""
        global _wsa_last_error
        s      = memory.read32((cpu.regs[ESP] + 4)  & 0xFFFFFFFF)
        lp_buf = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        length = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)

        entry = _socket_map.get(s)
        if entry is None or entry.py_sock is None:
            _wsa_last_error = WSAENOTSOCK
            cpu.regs[EAX] = SOCKET_ERROR
            cleanup_stdcall(cpu, memory, 24)
            return

        try:
            data, addr = entry.py_sock.recvfrom(length)
            for i, b in enumerate(data):
                memory.write8((lp_buf + i) & 0xFFFFFFFF, b)
            _wsa_last_error = 0
            cpu.regs[EAX] = len(data)
        except BlockingIOError:
            _wsa_last_error = WSAEWOULDBLOCK
            cpu.regs[EAX] = SOCKET_ERROR
        except OSError as exc:
            logger.warn("socket", f"recvfrom failed: {exc}")
            _wsa_last_error = WSAECONNREFUSED
            cpu.regs[EAX] = SOCKET_ERROR

        cleanup_stdcall(cpu, memory, 24)

    # ── Socket options ────────────────────────────────────────────────────────

    def _setsockopt(cpu: "CPU") -> None:
        """setsockopt(s, level, optname, optval, optlen) -> int."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 20)

    def _getsockopt(cpu: "CPU") -> None:
        """getsockopt(s, level, optname, optval, optlen) -> int."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 20)

    def _ioctlsocket(cpu: "CPU") -> None:
        """ioctlsocket(s, cmd, argp) -> int.

        Handles FIONBIO to set/clear non-blocking mode on the socket.
        """
        s    = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cmd  = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        argp = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)

        entry = _socket_map.get(s)
        if entry is not None and cmd == FIONBIO:
            nonblocking = bool(argp)
            entry.nonblocking = nonblocking
            if entry.py_sock:
                entry.py_sock.setblocking(not nonblocking)
            logger.debug("socket",
                f"ioctlsocket(0x{s:x}, FIONBIO, {argp}) -> nonblocking={nonblocking}")

        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 12)

    # ── select ────────────────────────────────────────────────────────────────

    def _select(cpu: "CPU") -> None:
        """select(nfds, readfds, writefds, exceptfds, timeout) -> int.

        Reads Win32 fd_set structs from memory, maps handles to Python sockets,
        calls select.select(), then writes the ready sets back.
        """
        global _wsa_last_error
        rd_ptr  = memory.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        wr_ptr  = memory.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        ex_ptr  = memory.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        tv_ptr  = memory.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)

        # Parse timeout (struct timeval)
        if tv_ptr:
            tv_sec  = memory.read32(tv_ptr & 0xFFFFFFFF)
            tv_usec = memory.read32((tv_ptr + 4) & 0xFFFFFFFF)
            timeout = tv_sec + tv_usec / 1_000_000
        else:
            timeout = None  # block indefinitely

        rd_handles = _read_fd_set(rd_ptr, memory)
        wr_handles = _read_fd_set(wr_ptr, memory)

        # Map handles to Python sockets (skip handles without connected sockets)
        def _py_socks(handles: list[int]) -> list[tuple[int, _socket_module.socket]]:
            return [
                (h, _socket_map[h].py_sock)
                for h in handles
                if h in _socket_map and _socket_map[h].py_sock is not None
            ]

        rd_pairs = _py_socks(rd_handles)
        wr_pairs = _py_socks(wr_handles)

        if not rd_pairs and not wr_pairs:
            # Nothing to wait on — return immediately with all sets empty
            if rd_ptr:
                _write_fd_set(rd_ptr, [], memory)
            if wr_ptr:
                _write_fd_set(wr_ptr, [], memory)
            if ex_ptr:
                _write_fd_set(ex_ptr, [], memory)
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 20)
            return

        try:
            rd_ready, wr_ready, _ = _select_module.select(
                [s for _, s in rd_pairs],
                [s for _, s in wr_pairs],
                [],
                timeout if timeout is not None else 0,
            )
        except OSError as exc:
            logger.warn("socket", f"select() failed: {exc}")
            _wsa_last_error = WSAENOTSOCK
            cpu.regs[EAX] = SOCKET_ERROR
            cleanup_stdcall(cpu, memory, 20)
            return

        rd_out = [h for h, s in rd_pairs if s in rd_ready]
        wr_out = [h for h, s in wr_pairs if s in wr_ready]

        if rd_ptr:
            _write_fd_set(rd_ptr, rd_out, memory)
        if wr_ptr:
            _write_fd_set(wr_ptr, wr_out, memory)
        if ex_ptr:
            _write_fd_set(ex_ptr, [], memory)

        cpu.regs[EAX] = len(rd_out) + len(wr_out)
        cleanup_stdcall(cpu, memory, 20)

    # ── Name resolution ───────────────────────────────────────────────────────

    def _inet_addr(cpu: "CPU") -> None:
        """inet_addr(cp) -> u_long (network byte order, or INADDR_NONE)."""
        lp_cp = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if not lp_cp:
            cpu.regs[EAX] = 0xFFFFFFFF  # INADDR_NONE
            cleanup_stdcall(cpu, memory, 4)
            return

        cp = read_cstring(lp_cp, memory)
        try:
            packed = _socket_module.inet_aton(cp)   # 4 bytes, big-endian
            # Return as little-endian DWORD so it stores correctly in sin_addr
            cpu.regs[EAX] = struct.unpack("<I", packed)[0]
        except OSError:
            cpu.regs[EAX] = 0xFFFFFFFF  # INADDR_NONE

        cleanup_stdcall(cpu, memory, 4)

    def _inet_ntoa(cpu: "CPU") -> None:
        """inet_ntoa(in) -> char*.  Returns NULL (no static char buffer)."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    def _gethostbyname(cpu: "CPU") -> None:
        """gethostbyname(name) -> HOSTENT* (into a static emulator buffer)."""
        global _wsa_last_error
        lp_name = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if not lp_name:
            _wsa_last_error = WSAHOST_NOT_FOUND
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 4)
            return

        name = read_cstring(lp_name, memory)
        try:
            ip_str = _socket_module.gethostbyname(name)
        except _socket_module.gaierror:
            logger.warn("socket", f"gethostbyname({name!r}) -> not found")
            _wsa_last_error = WSAHOST_NOT_FOUND
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 4)
            return

        logger.info("socket", f"gethostbyname({name!r}) -> {ip_str}")

        # Fill static hostent buffer
        # Pointers into _hostent_buf:
        name_ptr      = _hostent_buf + 16   # h_name string
        alias_ptr     = _hostent_buf + 80   # h_aliases = [NULL]
        addr_list_ptr = _hostent_buf + 84   # h_addr_list = [&ip, NULL]
        addr_ptr      = _hostent_buf + 92   # raw 4-byte IP

        # h_name
        name_bytes = name.encode("ascii", errors="replace") + b"\x00"
        for i, b in enumerate(name_bytes[:63]):
            memory.write8((_hostent_buf + 16 + i) & 0xFFFFFFFF, b)
        memory.write8((_hostent_buf + 16 + len(name_bytes) - 1) & 0xFFFFFFFF, 0)

        # h_aliases — NULL-terminated empty list
        memory.write32(alias_ptr & 0xFFFFFFFF, 0)

        # IP bytes (network byte order = big-endian)
        ip_bytes = _socket_module.inet_aton(ip_str)
        for i, b in enumerate(ip_bytes):
            memory.write8((addr_ptr + i) & 0xFFFFFFFF, b)

        # h_addr_list — one entry + NULL terminator
        memory.write32(addr_list_ptr & 0xFFFFFFFF,       addr_ptr)
        memory.write32((addr_list_ptr + 4) & 0xFFFFFFFF, 0)

        # HOSTENT struct itself
        memory.write32(_hostent_buf & 0xFFFFFFFF,        name_ptr)       # h_name
        memory.write32((_hostent_buf + 4) & 0xFFFFFFFF,  alias_ptr)      # h_aliases
        memory.write16((_hostent_buf + 8) & 0xFFFFFFFF,  AF_INET)        # h_addrtype
        memory.write16((_hostent_buf + 10) & 0xFFFFFFFF, 4)              # h_length
        memory.write32((_hostent_buf + 12) & 0xFFFFFFFF, addr_list_ptr)  # h_addr_list

        _wsa_last_error = 0
        cpu.regs[EAX] = _hostent_buf
        cleanup_stdcall(cpu, memory, 4)

    def _gethostbyaddr(cpu: "CPU") -> None:
        """gethostbyaddr(addr, len, type) -> HOSTENT*."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 12)

    def _gethostname(cpu: "CPU") -> None:
        """gethostname(name, namelen) -> int."""
        cpu.regs[EAX] = SOCKET_ERROR
        cleanup_stdcall(cpu, memory, 8)

    def _getservbyname(cpu: "CPU") -> None:
        """getservbyname(name, proto) -> SERVENT*."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    def _getservbyport(cpu: "CPU") -> None:
        """getservbyport(port, proto) -> SERVENT*."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 8)

    def _getprotobyname(cpu: "CPU") -> None:
        """getprotobyname(name) -> PROTOENT*."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    def _getprotobynumber(cpu: "CPU") -> None:
        """getprotobynumber(number) -> PROTOENT*."""
        cpu.regs[EAX] = 0
        cleanup_stdcall(cpu, memory, 4)

    # ── Byte-order conversion (must be correct — used in packet construction) ──

    def _htonl(cpu: "CPU") -> None:
        """htonl(hostlong) -> u_long (host-to-network 32-bit byte swap)."""
        v = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = struct.unpack(">I", struct.pack("<I", v))[0]
        cleanup_stdcall(cpu, memory, 4)

    def _htons(cpu: "CPU") -> None:
        """htons(hostshort) -> u_short (host-to-network 16-bit byte swap)."""
        v = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF) & 0xFFFF
        cpu.regs[EAX] = struct.unpack(">H", struct.pack("<H", v))[0]
        cleanup_stdcall(cpu, memory, 4)

    def _ntohl(cpu: "CPU") -> None:
        """ntohl(netlong) -> u_long (network-to-host 32-bit byte swap)."""
        v = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        cpu.regs[EAX] = struct.unpack("<I", struct.pack(">I", v))[0]
        cleanup_stdcall(cpu, memory, 4)

    def _ntohs(cpu: "CPU") -> None:
        """ntohs(netshort) -> u_short (network-to-host 16-bit byte swap)."""
        v = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF) & 0xFFFF
        cpu.regs[EAX] = struct.unpack("<H", struct.pack(">H", v))[0]
        cleanup_stdcall(cpu, memory, 4)

    # ── Registration ──────────────────────────────────────────────────────────

    named_handlers: dict[str, object] = {
        "WSAStartup":               _wsa_startup,
        "WSACleanup":               _wsa_cleanup,
        "WSAGetLastError":          _wsa_get_last_error,
        "WSASetLastError":          _wsa_set_last_error,
        "WSACancelBlockingCall":    _wsa_cancel_blocking_call,
        "WSAIsBlocking":            _wsa_is_blocking,
        "WSAAsyncSelect":           _wsa_async_select,
        "WSAAsyncGetHostByAddr":    _wsa_async_get_host_by_addr,
        "WSAAsyncGetHostByName":    _wsa_async_get_host_by_name,
        "WSACancelAsyncRequest":    _wsa_cancel_async_request,
        "__WSAFDIsSet":             _wsa_fd_is_set,
        "socket":                   _socket,
        "closesocket":              _closesocket,
        "bind":                     _bind,
        "listen":                   _listen,
        "connect":                  _connect,
        "accept":                   _accept,
        "shutdown":                 _shutdown,
        "getpeername":              _getpeername,
        "getsockname":              _getsockname,
        "setsockopt":               _setsockopt,
        "getsockopt":               _getsockopt,
        "ioctlsocket":              _ioctlsocket,
        "select":                   _select,
        "send":                     _send,
        "recv":                     _recv,
        "sendto":                   _sendto,
        "recvfrom":                 _recvfrom,
        "inet_addr":                _inet_addr,
        "inet_ntoa":                _inet_ntoa,
        "gethostbyname":            _gethostbyname,
        "gethostbyaddr":            _gethostbyaddr,
        "gethostname":              _gethostname,
        "getservbyname":            _getservbyname,
        "getservbyport":            _getservbyport,
        "getprotobyname":           _getprotobyname,
        "getprotobynumber":         _getprotobynumber,
        "htonl":                    _htonl,
        "htons":                    _htons,
        "ntohl":                    _ntohl,
        "ntohs":                    _ntohs,
    }

    for fn_name, handler in named_handlers.items():
        _reg(fn_name, handler)
        _reg(fn_name, handler, dll="ws2_32.dll")

    for ordinal, fn_name in ordinal_map.items():
        handler = named_handlers.get(fn_name)
        if handler is not None:
            alias = f"Ordinal #{ordinal}"
            _reg(alias, handler)
            _reg(alias, handler, dll="ws2_32.dll")

    logger.debug("handlers",
        f"wsock32/ws2_32: registered {len(named_handlers)} socket handlers "
        f"+ {len(ordinal_map)} ordinal aliases")
