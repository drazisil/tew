"""wininet.dll handler registrations.

Implements the core WinINet HTTP API used by authlogin.dll:
    InternetAttemptConnect, InternetOpenA, InternetConnectA,
    HttpOpenRequestA, InternetSetOptionA, HttpSendRequestA,
    HttpQueryInfoA, InternetReadFile, InternetCloseHandle.

HTTP requests are forwarded to a local server using Python's built-in
http.client.  If the server is not available the send call returns FALSE
and the game must handle the failure.

Win32 reference:
    https://learn.microsoft.com/en-us/windows/win32/wininet/wininet-reference
"""

from __future__ import annotations

import http.client
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import Win32Handlers, cleanup_stdcall
from tew.api._state import CRTState, read_cstring
from tew.logger import logger


# ── Win32 constants ───────────────────────────────────────────────────────────

HTTP_QUERY_STATUS_CODE  = 19
HTTP_QUERY_FLAG_NUMBER  = 0x20000000
INTERNET_SERVICE_HTTP   = 3

INTERNET_DEFAULT_HTTP_PORT  = 80
INTERNET_DEFAULT_HTTPS_PORT = 443


# ── Handle bookkeeping ────────────────────────────────────────────────────────

@dataclass
class InetSession:
    agent: str


@dataclass
class InetConnection:
    server: str
    port: int
    username: str
    password: str


@dataclass
class InetRequest:
    server: str
    port: int
    verb: str
    path: str
    headers: list[str] = field(default_factory=list)
    status_code: int = 0
    response_body: bytes = b""
    read_pos: int = 0


InetHandle = InetSession | InetConnection | InetRequest

_next_handle: int = 0xB000
_handle_map: dict[int, InetHandle] = {}


def _alloc_handle(obj: InetHandle) -> int:
    global _next_handle
    h = _next_handle
    _next_handle += 1
    _handle_map[h] = obj
    return h


# ── HTTP dispatch ─────────────────────────────────────────────────────────────

def _send_http(
    req: InetRequest,
    extra_headers: list[str],
    body: bytes,
) -> bool:
    """
    Forward *req* to a local HTTP server.  Returns True if the server
    responds (any status code), False if the connection itself fails.

    Populates req.status_code and req.response_body on success.
    """
    all_headers: dict[str, str] = {}
    for raw in req.headers + extra_headers:
        if ": " in raw:
            k, _, v = raw.partition(": ")
            all_headers[k.strip()] = v.strip()
        elif ":" in raw:
            k, _, v = raw.partition(":")
            all_headers[k.strip()] = v.strip()

    try:
        if req.port == INTERNET_DEFAULT_HTTPS_PORT:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(req.server, req.port, timeout=10, context=ctx)
        else:
            conn = http.client.HTTPConnection(req.server, req.port, timeout=10)

        conn.request(req.verb, req.path, body=body if body else None, headers=all_headers)
        resp = conn.getresponse()
        req.status_code  = resp.status
        req.response_body = resp.read()
        conn.close()
        logger.debug(
            "wininet",
            f"HTTP {req.verb} {req.server}:{req.port}{req.path}"
            f" -> {req.status_code} ({len(req.response_body)} bytes)",
        )
        return True
    except OSError as exc:
        logger.warn("wininet", f"HTTP {req.verb} {req.server}:{req.port}{req.path} -> connection failed: {exc}")
        return False


# ── Registration ──────────────────────────────────────────────────────────────

def register_wininet_handlers(
    stubs: Win32Handlers,
    memory: "Memory",
    state: CRTState,
) -> None:
    """Register all wininet.dll API handlers."""

    def _internet_attempt_connect(cpu: "CPU") -> None:
        """DWORD InternetAttemptConnect(DWORD dwReserved) → ERROR_SUCCESS"""
        cpu.regs[EAX] = 0   # ERROR_SUCCESS
        cleanup_stdcall(cpu, memory, 4)

    def _internet_open_a(cpu: "CPU") -> None:
        """
        HINTERNET InternetOpenA(LPCSTR lpszAgent, DWORD dwAccessType,
            LPCSTR lpszProxyName, LPCSTR lpszProxyBypass, DWORD dwFlags)
        """
        esp = cpu.regs[ESP]
        lp_agent = memory.read32((esp + 4) & 0xFFFFFFFF)
        agent    = read_cstring(lp_agent, memory) if lp_agent else "tew"
        handle   = _alloc_handle(InetSession(agent=agent))
        logger.debug("wininet", f"InternetOpenA(agent={agent!r}) -> 0x{handle:x}")
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 20)

    def _internet_connect_a(cpu: "CPU") -> None:
        """
        HINTERNET InternetConnectA(HINTERNET hInternet, LPCSTR lpszServerName,
            INTERNET_PORT nServerPort, LPCSTR lpszUserName, LPCSTR lpszPassword,
            DWORD dwService, DWORD dwFlags, DWORD_PTR dwContext)
        """
        esp = cpu.regs[ESP]
        lp_server  = memory.read32((esp +  8) & 0xFFFFFFFF)
        port_raw   = memory.read32((esp + 12) & 0xFFFFFFFF)
        lp_user    = memory.read32((esp + 16) & 0xFFFFFFFF)
        lp_pass    = memory.read32((esp + 20) & 0xFFFFFFFF)

        server   = read_cstring(lp_server, memory) if lp_server else ""
        port     = port_raw & 0xFFFF   # INTERNET_PORT is a WORD
        username = read_cstring(lp_user, memory) if lp_user else ""
        password = read_cstring(lp_pass, memory) if lp_pass else ""

        handle = _alloc_handle(InetConnection(
            server=server, port=port, username=username, password=password
        ))
        logger.debug("wininet", f"InternetConnectA({server}:{port}) -> 0x{handle:x}")
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 32)

    def _http_open_request_a(cpu: "CPU") -> None:
        """
        HINTERNET HttpOpenRequestA(HINTERNET hConnect, LPCSTR lpszVerb,
            LPCSTR lpszObjectName, LPCSTR lpszVersion, LPCSTR lpszReferrer,
            LPCSTR *lplpszAcceptTypes, DWORD dwFlags, DWORD_PTR dwContext)
        """
        esp = cpu.regs[ESP]
        h_connect  = memory.read32((esp +  4) & 0xFFFFFFFF)
        lp_verb    = memory.read32((esp +  8) & 0xFFFFFFFF)
        lp_path    = memory.read32((esp + 12) & 0xFFFFFFFF)

        verb  = read_cstring(lp_verb, memory) if lp_verb else "GET"
        path  = read_cstring(lp_path, memory) if lp_path else "/"
        # HTTP requires an absolute path starting with '/'.
        # Win32 games sometimes omit the leading slash (e.g. "AuthLogin?...").
        if path and not path.startswith("/"):
            path = "/" + path

        conn = _handle_map.get(h_connect)
        if not isinstance(conn, InetConnection):
            logger.warn("wininet", f"HttpOpenRequestA: invalid connection handle 0x{h_connect:x}")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 32)
            return

        handle = _alloc_handle(InetRequest(
            server=conn.server, port=conn.port, verb=verb, path=path,
        ))
        logger.debug("wininet", f"HttpOpenRequestA({verb} {path}) -> 0x{handle:x}")
        cpu.regs[EAX] = handle
        cleanup_stdcall(cpu, memory, 32)

    def _internet_set_option_a(cpu: "CPU") -> None:
        """BOOL InternetSetOptionA(HINTERNET, DWORD, LPVOID, DWORD) → TRUE"""
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 16)

    def _http_send_request_a(cpu: "CPU") -> None:
        """
        BOOL HttpSendRequestA(HINTERNET hRequest, LPCSTR lpszHeaders,
            DWORD dwHeadersLength, LPVOID lpOptional, DWORD dwOptionalLength)
        """
        esp = cpu.regs[ESP]
        h_request   = memory.read32((esp +  4) & 0xFFFFFFFF)
        lp_headers  = memory.read32((esp +  8) & 0xFFFFFFFF)
        dw_hdr_len  = memory.read32((esp + 12) & 0xFFFFFFFF)
        lp_optional = memory.read32((esp + 16) & 0xFFFFFFFF)
        dw_opt_len  = memory.read32((esp + 20) & 0xFFFFFFFF)

        req = _handle_map.get(h_request)
        if not isinstance(req, InetRequest):
            logger.warn("wininet", f"HttpSendRequestA: invalid request handle 0x{h_request:x}")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 20)
            return

        # Parse extra headers.
        # dw_hdr_len == 0xFFFFFFFF means the string is null-terminated;
        # otherwise it's the exact byte count.
        extra_headers: list[str] = []
        if lp_headers:
            if dw_hdr_len == 0xFFFFFFFF:
                hdr_text = read_cstring(lp_headers, memory, max_len=4096)
            else:
                hdr_text = "".join(
                    chr(memory.read8(lp_headers + i)) for i in range(dw_hdr_len)
                )
            for line in hdr_text.replace("\r\n", "\n").splitlines():
                line = line.strip()
                if line:
                    extra_headers.append(line)

        # Read optional body.
        body = b""
        if lp_optional and dw_opt_len:
            body = bytes(memory.read8(lp_optional + i) for i in range(dw_opt_len))

        req.read_pos = 0
        ok = _send_http(req, extra_headers, body)
        cpu.regs[EAX] = 1 if ok else 0
        cleanup_stdcall(cpu, memory, 20)

    def _http_query_info_a(cpu: "CPU") -> None:
        """
        BOOL HttpQueryInfoA(HINTERNET hRequest, DWORD dwInfoLevel,
            LPVOID lpBuffer, LPDWORD lpdwBufferLength, LPDWORD lpdwIndex)
        """
        esp = cpu.regs[ESP]
        h_request  = memory.read32((esp +  4) & 0xFFFFFFFF)
        dw_info    = memory.read32((esp +  8) & 0xFFFFFFFF)
        lp_buffer  = memory.read32((esp + 12) & 0xFFFFFFFF)
        lp_buf_len = memory.read32((esp + 16) & 0xFFFFFFFF)

        req = _handle_map.get(h_request)
        if not isinstance(req, InetRequest):
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 20)
            return

        # Mask off modifier flags to get the base info level.
        modifier  = dw_info & 0xFF000000
        info_type = dw_info & 0x00FFFFFF

        if info_type != HTTP_QUERY_STATUS_CODE:
            logger.warn("wininet", f"HttpQueryInfoA: unsupported dwInfoLevel=0x{dw_info:x}")
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 20)
            return

        if modifier & HTTP_QUERY_FLAG_NUMBER:
            # Return status as a DWORD.
            if lp_buffer and lp_buf_len:
                buf_len = memory.read32(lp_buf_len & 0xFFFFFFFF)
                if buf_len >= 4:
                    memory.write32(lp_buffer, req.status_code)
                    memory.write32(lp_buf_len, 4)
                    cpu.regs[EAX] = 1
                else:
                    cpu.regs[EAX] = 0
            else:
                cpu.regs[EAX] = 0
        else:
            # Return status as a null-terminated ASCII string.
            status_str = str(req.status_code).encode("ascii") + b"\x00"
            if lp_buffer and lp_buf_len:
                buf_len = memory.read32(lp_buf_len & 0xFFFFFFFF)
                if buf_len >= len(status_str):
                    for i, b in enumerate(status_str):
                        memory.write8(lp_buffer + i, b)
                    memory.write32(lp_buf_len, len(status_str))
                    cpu.regs[EAX] = 1
                else:
                    memory.write32(lp_buf_len, len(status_str))
                    cpu.regs[EAX] = 0
            else:
                cpu.regs[EAX] = 0

        cleanup_stdcall(cpu, memory, 20)

    def _internet_read_file(cpu: "CPU") -> None:
        """
        BOOL InternetReadFile(HINTERNET hFile, LPVOID lpBuffer,
            DWORD dwNumberOfBytesToRead, LPDWORD lpdwNumberOfBytesRead)
        """
        esp = cpu.regs[ESP]
        h_file      = memory.read32((esp +  4) & 0xFFFFFFFF)
        lp_buffer   = memory.read32((esp +  8) & 0xFFFFFFFF)
        dw_to_read  = memory.read32((esp + 12) & 0xFFFFFFFF)
        lp_bytes_rd = memory.read32((esp + 16) & 0xFFFFFFFF)

        req = _handle_map.get(h_file)
        if not isinstance(req, InetRequest):
            if lp_bytes_rd:
                memory.write32(lp_bytes_rd, 0)
            cpu.regs[EAX] = 0
            cleanup_stdcall(cpu, memory, 16)
            return

        available = len(req.response_body) - req.read_pos
        to_copy   = min(dw_to_read, available)
        chunk     = req.response_body[req.read_pos : req.read_pos + to_copy]
        for i, b in enumerate(chunk):
            memory.write8(lp_buffer + i, b)
        req.read_pos += to_copy

        if lp_bytes_rd:
            memory.write32(lp_bytes_rd, to_copy)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 16)

    def _internet_close_handle(cpu: "CPU") -> None:
        """BOOL InternetCloseHandle(HINTERNET hInternet) → TRUE"""
        h = memory.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        _handle_map.pop(h, None)
        cpu.regs[EAX] = 1
        cleanup_stdcall(cpu, memory, 4)

    stubs.register_handler("wininet.dll", "InternetAttemptConnect", _internet_attempt_connect)
    stubs.register_handler("wininet.dll", "InternetOpenA",          _internet_open_a)
    stubs.register_handler("wininet.dll", "InternetConnectA",       _internet_connect_a)
    stubs.register_handler("wininet.dll", "HttpOpenRequestA",       _http_open_request_a)
    stubs.register_handler("wininet.dll", "InternetSetOptionA",     _internet_set_option_a)
    stubs.register_handler("wininet.dll", "HttpSendRequestA",       _http_send_request_a)
    stubs.register_handler("wininet.dll", "HttpQueryInfoA",         _http_query_info_a)
    stubs.register_handler("wininet.dll", "InternetReadFile",       _internet_read_file)
    stubs.register_handler("wininet.dll", "InternetCloseHandle",    _internet_close_handle)
