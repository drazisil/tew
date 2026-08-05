"""Tests for wininet.dll HTTP API handlers.

_handle_map/_next_handle are module-level globals with no per-test reset
hook, so tests prefer handler-returned handles over hardcoded constants
(sidesteps collision risk) -- hardcoded values are only used for
deliberately-invalid/unknown-handle negative tests. Header-parsing
edge cases mock http.client.HTTPConnection so the composed headers dict
can be asserted on directly; the "server unreachable" test uses a real
closed loopback port to exercise the actual OSError branch.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tew.api._state import CRTState
from tew.api.wininet_handlers import (
    register_wininet_handlers,
    _handle_map,
    InetSession,
    InetConnection,
    InetRequest,
    HTTP_QUERY_STATUS_CODE,
    HTTP_QUERY_FLAG_NUMBER,
)
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP


class _StubHandlers:
    def __init__(self):
        self._h: dict = {}

    def register_handler(self, dll, name, fn):
        self._h[(dll, name)] = fn

    def get(self, dll, name):
        return self._h[(dll, name)]


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False


MEM_SIZE = 4 * 1024 * 1024
STACK    = 0x200000
BUF_A    = 0x300000
BUF_B    = 0x310000

INVALID_HANDLE = 0xFFFF0000


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_wininet_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    return cpu, mem, state, stubs


def call(stubs, cpu, mem, name, args):
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    for i, val in enumerate(args):
        mem.write32(STACK + 4 + i * 4, val)
    stubs.get("wininet.dll", name)(cpu)


def write_cstring(mem, addr, s: str) -> None:
    data = s.encode("ascii") + b"\x00"
    for i, b in enumerate(data):
        mem.write8(addr + i, b)


def open_request(stubs, cpu, mem, server="127.0.0.1", port=1, verb="GET", path="/x"):
    """InternetOpenA -> InternetConnectA -> HttpOpenRequestA, returns the request handle."""
    call(stubs, cpu, mem, "InternetOpenA", [0])
    write_cstring(mem, BUF_A, server)
    write_cstring(mem, BUF_B, verb)
    call(stubs, cpu, mem, "InternetConnectA", [0, BUF_A, port, 0, 0, 3, 0, 0])
    h_connect = cpu.regs[EAX]
    path_addr = 0x320000
    write_cstring(mem, path_addr, path)
    call(stubs, cpu, mem, "HttpOpenRequestA", [h_connect, BUF_B, path_addr, 0, 0, 0, 0, 0])
    return cpu.regs[EAX]


# ── InternetAttemptConnect ─────────────────────────────────────────────────────

class TestInternetAttemptConnect:

    def test_always_returns_success(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "InternetAttemptConnect", [0])
        assert cpu.regs[EAX] == 0


# ── InternetOpenA ──────────────────────────────────────────────────────────────

class TestInternetOpenA:

    def test_real_agent_string_tracked(self, env):
        cpu, mem, state, stubs = env
        write_cstring(mem, BUF_A, "MyAgent/1.0")
        call(stubs, cpu, mem, "InternetOpenA", [BUF_A, 0, 0, 0, 0])
        handle = cpu.regs[EAX]
        entry = _handle_map[handle]
        assert isinstance(entry, InetSession)
        assert entry.agent == "MyAgent/1.0"

    def test_null_agent_defaults_to_tew(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "InternetOpenA", [0, 0, 0, 0, 0])
        handle = cpu.regs[EAX]
        assert _handle_map[handle].agent == "tew"

    def test_stdcall_cleanup(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "InternetOpenA", [0, 0, 0, 0, 0])
        assert cpu.regs[ESP] == STACK + 20


# ── InternetConnectA ───────────────────────────────────────────────────────────

class TestInternetConnectA:

    def test_server_and_credentials_tracked(self, env):
        cpu, mem, state, stubs = env
        write_cstring(mem, BUF_A, "example.com")
        call(stubs, cpu, mem, "InternetConnectA", [0, BUF_A, 8080, 0, 0, 3, 0, 0])
        handle = cpu.regs[EAX]
        entry = _handle_map[handle]
        assert isinstance(entry, InetConnection)
        assert entry.server == "example.com"
        assert entry.port == 8080

    def test_port_masked_to_16_bits(self, env):
        cpu, mem, state, stubs = env
        write_cstring(mem, BUF_A, "example.com")
        garbage_high_bits_port = 0x12340050  # low 16 bits = 0x0050 = 80
        call(stubs, cpu, mem, "InternetConnectA", [0, BUF_A, garbage_high_bits_port, 0, 0, 3, 0, 0])
        handle = cpu.regs[EAX]
        assert _handle_map[handle].port == 80

    def test_stdcall_cleanup(self, env):
        cpu, mem, state, stubs = env
        write_cstring(mem, BUF_A, "example.com")
        call(stubs, cpu, mem, "InternetConnectA", [0, BUF_A, 80, 0, 0, 3, 0, 0])
        assert cpu.regs[ESP] == STACK + 32


# ── HttpOpenRequestA ───────────────────────────────────────────────────────────

class TestHttpOpenRequestA:

    def test_valid_connection_allocates_request(self, env):
        cpu, mem, state, stubs = env
        write_cstring(mem, BUF_A, "example.com")
        call(stubs, cpu, mem, "InternetConnectA", [0, BUF_A, 80, 0, 0, 3, 0, 0])
        h_connect = cpu.regs[EAX]
        write_cstring(mem, BUF_B, "POST")
        path_addr = 0x320000
        write_cstring(mem, path_addr, "/api")
        call(stubs, cpu, mem, "HttpOpenRequestA", [h_connect, BUF_B, path_addr, 0, 0, 0, 0, 0])
        handle = cpu.regs[EAX]
        entry = _handle_map[handle]
        assert isinstance(entry, InetRequest)
        assert entry.verb == "POST"
        assert entry.path == "/api"

    def test_path_without_leading_slash_gets_one_prepended(self, env):
        cpu, mem, state, stubs = env
        write_cstring(mem, BUF_A, "example.com")
        call(stubs, cpu, mem, "InternetConnectA", [0, BUF_A, 80, 0, 0, 3, 0, 0])
        h_connect = cpu.regs[EAX]
        write_cstring(mem, BUF_B, "GET")
        path_addr = 0x320000
        write_cstring(mem, path_addr, "AuthLogin?x=1")
        call(stubs, cpu, mem, "HttpOpenRequestA", [h_connect, BUF_B, path_addr, 0, 0, 0, 0, 0])
        handle = cpu.regs[EAX]
        assert _handle_map[handle].path == "/AuthLogin?x=1"

    def test_invalid_connection_handle_returns_zero(self, env):
        cpu, mem, state, stubs = env
        write_cstring(mem, BUF_B, "GET")
        call(stubs, cpu, mem, "HttpOpenRequestA", [INVALID_HANDLE, BUF_B, 0, 0, 0, 0, 0, 0])
        assert cpu.regs[EAX] == 0


# ── InternetSetOptionA ─────────────────────────────────────────────────────────

class TestInternetSetOptionA:

    def test_always_returns_true(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "InternetSetOptionA", [0, 0, 0, 0])
        assert cpu.regs[EAX] == 1


# ── HttpSendRequestA ───────────────────────────────────────────────────────────

class TestHttpSendRequestA:

    def test_invalid_handle_returns_false(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HttpSendRequestA", [INVALID_HANDLE, 0, 0, 0, 0])
        assert cpu.regs[EAX] == 0

    def test_unreachable_host_returns_false(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem, server="127.0.0.1", port=1)
        req = _handle_map[handle]
        call(stubs, cpu, mem, "HttpSendRequestA", [handle, 0, 0, 0, 0])
        assert cpu.regs[EAX] == 0
        assert req.status_code == 0
        assert req.response_body == b""

    def test_null_terminated_and_explicit_length_headers_match(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        hdr_text = "X-Foo: bar\r\nX-Baz: qux\r\n"

        captured = []

        def fake_connection(*a, **kw):
            conn = MagicMock()
            conn.getresponse.return_value = MagicMock(status=200, read=lambda: b"")
            captured.append(conn)
            return conn

        with patch("http.client.HTTPConnection", side_effect=fake_connection):
            write_cstring(mem, BUF_A, hdr_text)
            call(stubs, cpu, mem, "HttpSendRequestA", [handle, BUF_A, 0xFFFFFFFF, 0, 0])
            headers_null_terminated = captured[-1].request.call_args.kwargs["headers"]

            for i, ch in enumerate(hdr_text):
                mem.write8(BUF_A + i, ord(ch))
            call(stubs, cpu, mem, "HttpSendRequestA", [handle, BUF_A, len(hdr_text), 0, 0])
            headers_explicit_length = captured[-1].request.call_args.kwargs["headers"]

        assert headers_null_terminated == headers_explicit_length
        assert headers_null_terminated == {"X-Foo": "bar", "X-Baz": "qux"}

    def test_colon_with_and_without_space_both_split(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        hdr_text = "X-NoSpace:value1\r\nX-WithSpace: value2\r\n"
        write_cstring(mem, BUF_A, hdr_text)

        captured = []

        def fake_connection(*a, **kw):
            conn = MagicMock()
            conn.getresponse.return_value = MagicMock(status=200, read=lambda: b"")
            captured.append(conn)
            return conn

        with patch("http.client.HTTPConnection", side_effect=fake_connection):
            call(stubs, cpu, mem, "HttpSendRequestA", [handle, BUF_A, 0xFFFFFFFF, 0, 0])

        headers = captured[-1].request.call_args.kwargs["headers"]
        assert headers == {"X-NoSpace": "value1", "X-WithSpace": "value2"}

    def test_header_line_without_any_colon_is_skipped(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        hdr_text = "GarbageLineNoColon\r\nX-Foo: bar\r\n"
        write_cstring(mem, BUF_A, hdr_text)

        captured = []

        def fake_connection(*a, **kw):
            conn = MagicMock()
            conn.getresponse.return_value = MagicMock(status=200, read=lambda: b"")
            captured.append(conn)
            return conn

        with patch("http.client.HTTPConnection", side_effect=fake_connection):
            call(stubs, cpu, mem, "HttpSendRequestA", [handle, BUF_A, 0xFFFFFFFF, 0, 0])

        headers = captured[-1].request.call_args.kwargs["headers"]
        assert headers == {"X-Foo": "bar"}

    def test_blank_line_in_headers_is_skipped(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        hdr_text = "\r\n\r\nX-Foo: bar\r\n"
        write_cstring(mem, BUF_A, hdr_text)

        captured = []

        def fake_connection(*a, **kw):
            conn = MagicMock()
            conn.getresponse.return_value = MagicMock(status=200, read=lambda: b"")
            captured.append(conn)
            return conn

        with patch("http.client.HTTPConnection", side_effect=fake_connection):
            call(stubs, cpu, mem, "HttpSendRequestA", [handle, BUF_A, 0xFFFFFFFF, 0, 0])

        headers = captured[-1].request.call_args.kwargs["headers"]
        assert headers == {"X-Foo": "bar"}

    def test_optional_body_forwarded_to_request(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        body = b"field=value"
        for i, b in enumerate(body):
            mem.write8(BUF_A + i, b)

        captured = []

        def fake_connection(*a, **kw):
            conn = MagicMock()
            conn.getresponse.return_value = MagicMock(status=200, read=lambda: b"")
            captured.append(conn)
            return conn

        with patch("http.client.HTTPConnection", side_effect=fake_connection):
            call(stubs, cpu, mem, "HttpSendRequestA", [handle, 0, 0, BUF_A, len(body)])

        assert captured[-1].request.call_args.kwargs["body"] == body

    def test_https_port_uses_https_connection(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem, port=443)

        captured = []

        def fake_connection(*a, **kw):
            conn = MagicMock()
            conn.getresponse.return_value = MagicMock(status=200, read=lambda: b"")
            captured.append(conn)
            return conn

        with patch("http.client.HTTPSConnection", side_effect=fake_connection) as mock_https:
            call(stubs, cpu, mem, "HttpSendRequestA", [handle, 0, 0, 0, 0])

        mock_https.assert_called_once()
        assert cpu.regs[EAX] == 1


# ── HttpQueryInfoA ─────────────────────────────────────────────────────────────

class TestHttpQueryInfoA:

    def test_invalid_handle_returns_false(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "HttpQueryInfoA", [INVALID_HANDLE, HTTP_QUERY_STATUS_CODE, 0, 0, 0])
        assert cpu.regs[EAX] == 0

    def test_unsupported_info_level_returns_false(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        call(stubs, cpu, mem, "HttpQueryInfoA", [handle, 999, 0, 0, 0])
        assert cpu.regs[EAX] == 0

    def test_number_mode_sufficient_buffer(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        _handle_map[handle].status_code = 404
        mem.write32(BUF_B, 4)  # buffer length = 4 bytes
        info_level = HTTP_QUERY_STATUS_CODE | HTTP_QUERY_FLAG_NUMBER
        call(stubs, cpu, mem, "HttpQueryInfoA", [handle, info_level, BUF_A, BUF_B, 0])
        assert cpu.regs[EAX] == 1
        assert mem.read32(BUF_A) == 404
        assert mem.read32(BUF_B) == 4

    def test_number_mode_buffer_too_small(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        _handle_map[handle].status_code = 404
        mem.write32(BUF_B, 2)  # too small
        info_level = HTTP_QUERY_STATUS_CODE | HTTP_QUERY_FLAG_NUMBER
        call(stubs, cpu, mem, "HttpQueryInfoA", [handle, info_level, BUF_A, BUF_B, 0])
        assert cpu.regs[EAX] == 0

    def test_string_mode_sufficient_buffer(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        _handle_map[handle].status_code = 200
        mem.write32(BUF_B, 16)
        call(stubs, cpu, mem, "HttpQueryInfoA", [handle, HTTP_QUERY_STATUS_CODE, BUF_A, BUF_B, 0])
        assert cpu.regs[EAX] == 1
        out = bytearray()
        while True:
            b = mem.read8(BUF_A + len(out))
            if b == 0:
                break
            out.append(b)
        assert out == b"200"

    def test_string_mode_buffer_too_small_reports_required_size(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        _handle_map[handle].status_code = 200
        mem.write32(BUF_B, 1)  # too small for "200\0" (4 bytes)
        call(stubs, cpu, mem, "HttpQueryInfoA", [handle, HTTP_QUERY_STATUS_CODE, BUF_A, BUF_B, 0])
        assert cpu.regs[EAX] == 0
        assert mem.read32(BUF_B) == 4  # required size written back

    def test_number_mode_null_buffer_pointer_returns_false(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        info_level = HTTP_QUERY_STATUS_CODE | HTTP_QUERY_FLAG_NUMBER
        call(stubs, cpu, mem, "HttpQueryInfoA", [handle, info_level, 0, 0, 0])
        assert cpu.regs[EAX] == 0

    def test_string_mode_null_buffer_pointer_returns_false(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        call(stubs, cpu, mem, "HttpQueryInfoA", [handle, HTTP_QUERY_STATUS_CODE, 0, 0, 0])
        assert cpu.regs[EAX] == 0


# ── InternetReadFile ───────────────────────────────────────────────────────────

class TestInternetReadFile:

    def test_invalid_handle_writes_zero_bytes_read(self, env):
        cpu, mem, state, stubs = env
        mem.write32(BUF_B, 0xFFFFFFFF)  # pre-dirty
        call(stubs, cpu, mem, "InternetReadFile", [INVALID_HANDLE, BUF_A, 16, BUF_B])
        assert cpu.regs[EAX] == 0
        assert mem.read32(BUF_B) == 0

    def test_invalid_handle_null_bytes_read_pointer_does_not_crash(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "InternetReadFile", [INVALID_HANDLE, BUF_A, 16, 0])  # must not raise
        assert cpu.regs[EAX] == 0

    def test_valid_handle_null_bytes_read_pointer_still_copies_data(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        _handle_map[handle].response_body = b"hello"
        call(stubs, cpu, mem, "InternetReadFile", [handle, BUF_A, 5, 0])  # must not raise
        assert cpu.regs[EAX] == 1
        assert bytes(mem.read8(BUF_A + i) for i in range(5)) == b"hello"

    def test_reads_chunk_and_advances_position(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        _handle_map[handle].response_body = b"hello world"
        call(stubs, cpu, mem, "InternetReadFile", [handle, BUF_A, 5, BUF_B])
        assert cpu.regs[EAX] == 1
        assert mem.read32(BUF_B) == 5
        assert bytes(mem.read8(BUF_A + i) for i in range(5)) == b"hello"
        assert _handle_map[handle].read_pos == 5

    def test_repeated_reads_exhaust_body(self, env):
        cpu, mem, state, stubs = env
        handle = open_request(stubs, cpu, mem)
        _handle_map[handle].response_body = b"hi"
        call(stubs, cpu, mem, "InternetReadFile", [handle, BUF_A, 100, BUF_B])
        assert mem.read32(BUF_B) == 2
        call(stubs, cpu, mem, "InternetReadFile", [handle, BUF_A, 100, BUF_B])
        assert mem.read32(BUF_B) == 0  # nothing left to read


# ── InternetCloseHandle ────────────────────────────────────────────────────────

class TestInternetCloseHandle:

    def test_removes_handle_from_map(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "InternetOpenA", [0, 0, 0, 0, 0])
        handle = cpu.regs[EAX]
        assert handle in _handle_map
        call(stubs, cpu, mem, "InternetCloseHandle", [handle])
        assert handle not in _handle_map

    def test_closing_unknown_handle_does_not_raise(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "InternetCloseHandle", [INVALID_HANDLE])  # must not raise
        assert cpu.regs[EAX] == 1
