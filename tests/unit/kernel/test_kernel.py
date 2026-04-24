"""Unit tests for tew.kernel.kernel.Kernel."""

from __future__ import annotations

import socket as _socket_module
from unittest.mock import MagicMock, patch

import pytest

from tew.kernel.kernel import Kernel, FD_CONNECT, FD_READ, FD_WRITE


# ── Minimal state stub ────────────────────────────────────────────────────────

class _FakeWindowManager:
    def __init__(self):
        self.posted: list = []

    def post_message(self, hwnd, msg, wparam, lparam):
        self.posted.append((hwnd, msg, wparam, lparam))
        return True


class _FakeEventHandle:
    def __init__(self):
        self.signaled = False


class _FakeState:
    def __init__(self):
        self.window_manager = _FakeWindowManager()
        self.kernel_handle_map: dict = {}
        self.scheduler = MagicMock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_kernel() -> tuple[Kernel, _FakeState]:
    state = _FakeState()
    kernel = Kernel(state)
    return kernel, state


# ── register_async_select / unregister ───────────────────────────────────────

def test_register_async_select_stores_registration():
    k, _ = _make_kernel()
    k.register_async_select(0x100, hwnd=0x1000, msg=0x401, events=FD_CONNECT)
    assert 0x100 in k._async_select
    reg = k._async_select[0x100]
    assert reg.hwnd == 0x1000
    assert reg.msg == 0x401
    assert reg.events == FD_CONNECT


def test_register_async_select_events_zero_removes():
    k, _ = _make_kernel()
    k.register_async_select(0x100, hwnd=0x1000, msg=0x401, events=FD_CONNECT)
    k.register_async_select(0x100, hwnd=0x1000, msg=0x401, events=0)
    assert 0x100 not in k._async_select


def test_register_event_select_stores():
    k, _ = _make_kernel()
    k.register_event_select(0x101, event_handle=0x7010, events=FD_READ)
    assert 0x101 in k._event_select
    assert k._event_select[0x101].event_handle == 0x7010


def test_unregister_socket_clears_all():
    k, _ = _make_kernel()
    k.register_async_select(0x100, 0x1000, 0x401, FD_CONNECT)
    k.register_event_select(0x100, 0x7010, FD_READ)
    k._connect_fired.add(0x100)
    k.unregister_socket(0x100)
    assert 0x100 not in k._async_select
    assert 0x100 not in k._event_select
    assert 0x100 not in k._connect_fired


# ── tick / deliver ────────────────────────────────────────────────────────────

def test_tick_no_registrations_is_noop():
    k, state = _make_kernel()
    k.tick()  # must not raise
    assert state.window_manager.posted == []


def _make_connected_pair():
    """Return (server_sock, client_sock) on localhost."""
    srv = _socket_module.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    cli = _socket_module.socket()
    cli.connect(("127.0.0.1", port))
    conn, _ = srv.accept()
    srv.close()
    return conn, cli


def test_tick_async_select_fd_read_posts_message():
    """A socket with pending data triggers an AsyncSelect PostMessage."""
    from tew.api.wsock32_handlers import SocketEntry

    k, state = _make_kernel()
    server, client = _make_connected_pair()
    try:
        client.send(b"hello")

        fake_entry = SocketEntry(af=2, type_=1, proto=0, py_sock=server)
        fake_map = {0x100: fake_entry}

        k.register_async_select(0x100, hwnd=0x1000, msg=0x8001, events=FD_READ)

        with patch("tew.kernel.kernel._sel.select",
                   wraps=__import__("select").select):
            with patch("tew.api.wsock32_handlers._socket_map", fake_map):
                k.tick()

        assert len(state.window_manager.posted) == 1
        hwnd, msg, wparam, _ = state.window_manager.posted[0]
        assert hwnd == 0x1000
        assert msg  == 0x8001
        assert wparam == 0x100   # socket handle
    finally:
        server.close()
        client.close()


def test_tick_event_select_fd_connect_signals_event():
    """A writable socket triggers EventSelect SetEvent + unblock_handle."""
    from tew.api.wsock32_handlers import SocketEntry
    from tew.api._state import EventHandle

    k, state = _make_kernel()
    server, client = _make_connected_pair()
    try:
        event_obj = EventHandle(signaled=False, manual_reset=False)
        state.kernel_handle_map[0x7010] = event_obj

        fake_entry = SocketEntry(af=2, type_=1, proto=0, py_sock=client)
        fake_map = {0x101: fake_entry}

        k.register_event_select(0x101, event_handle=0x7010, events=FD_CONNECT)

        with patch("tew.api.wsock32_handlers._socket_map", fake_map):
            k.tick()

        assert event_obj.signaled is True
        state.scheduler.unblock_handle.assert_called_once_with(0x7010)
    finally:
        server.close()
        client.close()


def test_tick_fd_connect_fires_only_once():
    """FD_CONNECT is a one-shot event — second tick must not re-deliver."""
    from tew.api.wsock32_handlers import SocketEntry
    from tew.api._state import EventHandle

    k, state = _make_kernel()
    server, client = _make_connected_pair()
    try:
        event_obj = EventHandle(signaled=False, manual_reset=False)
        state.kernel_handle_map[0x7010] = event_obj

        fake_entry = SocketEntry(af=2, type_=1, proto=0, py_sock=client)
        fake_map = {0x101: fake_entry}
        k.register_event_select(0x101, event_handle=0x7010, events=FD_CONNECT)

        with patch("tew.api.wsock32_handlers._socket_map", fake_map):
            k.tick()
            event_obj.signaled = False   # reset to verify second tick doesn't re-fire
            k.tick()

        assert event_obj.signaled is False
        state.scheduler.unblock_handle.assert_called_once_with(0x7010)
    finally:
        server.close()
        client.close()
