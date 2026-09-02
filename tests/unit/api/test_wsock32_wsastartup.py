"""Tests for wsock32.dll!WSAStartup -- real bug found 2026-09-02 chasing a live
MessagePool::Get(NULL) crash on DBThread.

Real WSAStartup echoes wVersionRequested straight back as wVersion; tew
previously hardcoded both wVersion and wHighVersion to 0x0202. MCity_d.exe's
TCPMgr::Initialize (0x00a7a4fe) requests MAKEWORD(1,1)=0x0101 and does an
exact byte-for-byte match of the returned wVersion against what it asked
for before proceeding to construct its three MessagePool objects -- with
wVersion always reported back as 0x0202, that check always failed, silently
skipping all three MessagePool::Initialize calls every run, leaving
CommMgr's pool pointers permanently NULL.
"""
from __future__ import annotations

from tew.api._state import CRTState
from tew.api.win32_handlers import Win32Handlers
from tew.api.wsock32_handlers import register_wsock32_handlers
from tew.hardware.cpu_zig import EAX, ESP
from tew.hardware.memory import Memory

MEM_SIZE     = 4 * 1024 * 1024
STACK        = 0x00200000
WSADATA_ADDR = 0x00300000


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False


def _env():
    mem = Memory(MEM_SIZE)
    state = CRTState()
    stubs = Win32Handlers(mem)
    register_wsock32_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    return cpu, mem, state, stubs


def _call_wsa_startup(stubs, cpu, mem, w_version_requested: int) -> None:
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)                      # return address
    mem.write32(STACK + 4, w_version_requested)
    mem.write32(STACK + 8, WSADATA_ADDR)
    stubs._handlers["wsock32.dll!WSAStartup"].handler(cpu)


class TestWsaStartupEchoesRequestedVersion:

    def test_returns_success(self):
        cpu, mem, state, stubs = _env()
        _call_wsa_startup(stubs, cpu, mem, 0x0101)
        assert cpu.regs[EAX] == 0

    def test_wversion_echoes_requested_1_1(self):
        # The exact real-world shape: MAKEWORD(1,1).
        cpu, mem, state, stubs = _env()
        _call_wsa_startup(stubs, cpu, mem, 0x0101)
        w_version = mem.read16(WSADATA_ADDR)
        assert w_version == 0x0101

    def test_wversion_echoes_requested_2_2(self):
        cpu, mem, state, stubs = _env()
        _call_wsa_startup(stubs, cpu, mem, 0x0202)
        w_version = mem.read16(WSADATA_ADDR)
        assert w_version == 0x0202

    def test_whighversion_always_reports_dll_max_regardless_of_request(self):
        # wHighVersion reports the DLL's own capability, not an echo of the
        # request -- unlike wVersion, this one really is a constant.
        cpu, mem, state, stubs = _env()
        _call_wsa_startup(stubs, cpu, mem, 0x0101)
        w_high_version = mem.read16(WSADATA_ADDR + 2)
        assert w_high_version == 0x0202

    def test_does_not_crash_with_null_lpwsadata(self):
        cpu, mem, state, stubs = _env()
        cpu.regs[ESP] = STACK
        mem.write32(STACK, 0xDEAD)
        mem.write32(STACK + 4, 0x0101)
        mem.write32(STACK + 8, 0)  # lpWSAData == NULL
        stubs._handlers["wsock32.dll!WSAStartup"].handler(cpu)  # must not raise
        assert cpu.regs[EAX] == 0
