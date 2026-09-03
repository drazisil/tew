"""Tests for dsound.dll IDirectSound/IDirectSoundBuffer buffer lifecycle
and state management -- everything except Buf::Lock's circular-buffer
cursor math (test_dsound_buffer_lock_unlock.py) and the SDL-audio-backed
Buf::Play path (test_dsound_play_sdl.py).

_ds_buffers/_next_buf_idx are module-level globals with no per-test reset
hook -- tests use handler-returned handles/indices rather than hardcoded
constants, matching the convention established in test_dinput_handlers.py.
"""
from __future__ import annotations

import pytest

from tew.api._state import CRTState
from tew.api.dsound_handlers import (
    register_dsound_handlers,
    _ds_buffers,
    DS_OBJ,
    DS_VTABLE,
    DS_BUF_VTABLE,
    DS_OK,
    DSERR_INVALIDPARAM,
    DSERR_ALREADYINITIALIZED,
    DSBCAPS_PRIMARYBUFFER,
    DSBSTATUS_PLAYING,
    DSBSTATUS_LOOPING,
)
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP


class _StubHandlers:
    def __init__(self):
        self._h: dict = {}

    def register_handler(self, dll, name, fn):
        self._h[f"{dll.lower()}!{name}"] = fn

    def get(self, dll, name):
        return self._h[f"{dll.lower()}!{name}"]

    def get_handler_address(self, dll, name):
        return 0


class _FakeCPU:
    def __init__(self):
        self.regs = [0] * 8
        self.halted = False


MEM_SIZE = 272 * 1024 * 1024  # must exceed the D3D8 private heap limit (0x10000000)
STACK    = 0x200000
BUF_A    = 0x300000
BUF_B    = 0x310000
DESC_ADDR = 0x320000
WFX_ADDR  = 0x330000


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_dsound_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    return cpu, mem, state, stubs


def call(stubs, cpu, mem, dll, name, args):
    """Plain stdcall handler (DirectSoundCreate and its aliases)."""
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    for i, val in enumerate(args):
        mem.write32(STACK + 4 + i * 4, val)
    stubs.get(dll, name)(cpu)


def com_call(stubs, cpu, mem, dll, name, this_ptr, args):
    """COM-style call: [ESP]=ret, [ESP+4]=this, [ESP+8..]=args."""
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, this_ptr)
    for i, val in enumerate(args):
        mem.write32(STACK + 8 + i * 4, val)
    stubs.get(dll, name)(cpu)


def write_dsbufferdesc(mem, addr, flags=0, buf_bytes=0, wfx_ptr=0):
    mem.write32(addr, 0)          # dwSize (unused by handler)
    mem.write32(addr + 4, flags)
    mem.write32(addr + 8, buf_bytes)
    mem.write32(addr + 16, wfx_ptr)


def write_wfx(mem, addr, sample_rate=44100, channels=2, bits=16):
    mem.write16(addr + 2, channels)
    mem.write32(addr + 4, sample_rate)
    mem.write16(addr + 14, bits)


def create_secondary_buffer(stubs, cpu, mem, buf_bytes=1024, sample_rate=44100, channels=2, bits=16):
    """Create a secondary sound buffer through the real handler chain, return the COM obj addr."""
    write_wfx(mem, WFX_ADDR, sample_rate, channels, bits)
    write_dsbufferdesc(mem, DESC_ADDR, flags=0, buf_bytes=buf_bytes, wfx_ptr=WFX_ADDR)
    com_call(stubs, cpu, mem, "dsound.dll", "DS::CreateSoundBuffer", DS_OBJ, [DESC_ADDR, BUF_A, 0])
    return mem.read32(BUF_A)


# ── DS::QueryInterface / AddRef / Release ──────────────────────────────────────

class TestDSQueryInterface:

    def test_writes_ds_obj_to_ppv(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::QueryInterface", DS_OBJ, [0, BUF_A])
        assert mem.read32(BUF_A) == DS_OBJ

    def test_returns_ds_ok(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::QueryInterface", DS_OBJ, [0, BUF_A])
        assert cpu.regs[EAX] == DS_OK

    def test_null_ppv_does_not_crash(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::QueryInterface", DS_OBJ, [0, 0])  # must not raise
        assert cpu.regs[EAX] == DS_OK


class TestDSAddRefRelease:

    def test_addref_returns_two(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::AddRef", DS_OBJ, [])
        assert cpu.regs[EAX] == 2

    def test_release_returns_one(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::Release", DS_OBJ, [])
        assert cpu.regs[EAX] == 1


# ── DS::CreateSoundBuffer ────────────────────────────────────────────────────

class TestCreateSoundBuffer:

    def test_null_desc_returns_invalidparam(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::CreateSoundBuffer", DS_OBJ, [0, BUF_A, 0])
        assert cpu.regs[EAX] == DSERR_INVALIDPARAM

    def test_null_out_pointer_returns_invalidparam(self, env):
        cpu, mem, state, stubs = env
        write_dsbufferdesc(mem, DESC_ADDR, buf_bytes=1024, wfx_ptr=WFX_ADDR)
        com_call(stubs, cpu, mem, "dsound.dll", "DS::CreateSoundBuffer", DS_OBJ, [DESC_ADDR, 0, 0])
        assert cpu.regs[EAX] == DSERR_INVALIDPARAM

    def test_primary_buffer_has_zero_pcm_addr(self, env):
        cpu, mem, state, stubs = env
        write_dsbufferdesc(mem, DESC_ADDR, flags=DSBCAPS_PRIMARYBUFFER)
        com_call(stubs, cpu, mem, "dsound.dll", "DS::CreateSoundBuffer", DS_OBJ, [DESC_ADDR, BUF_A, 0])
        obj = mem.read32(BUF_A)
        assert mem.read32(obj + 4) == 0  # pcm_addr
        assert cpu.regs[EAX] == DS_OK

    def test_primary_buffer_does_not_call_simple_alloc(self, env):
        cpu, mem, state, stubs = env
        before = state.next_heap_alloc
        write_dsbufferdesc(mem, DESC_ADDR, flags=DSBCAPS_PRIMARYBUFFER)
        com_call(stubs, cpu, mem, "dsound.dll", "DS::CreateSoundBuffer", DS_OBJ, [DESC_ADDR, BUF_A, 0])
        assert state.next_heap_alloc == before

    def test_secondary_buffer_zero_bytes_returns_invalidparam(self, env):
        cpu, mem, state, stubs = env
        write_dsbufferdesc(mem, DESC_ADDR, flags=0, buf_bytes=0, wfx_ptr=WFX_ADDR)
        com_call(stubs, cpu, mem, "dsound.dll", "DS::CreateSoundBuffer", DS_OBJ, [DESC_ADDR, BUF_A, 0])
        assert cpu.regs[EAX] == DSERR_INVALIDPARAM

    def test_secondary_buffer_allocates_real_pcm(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=512)
        pcm_addr = mem.read32(obj + 4)
        assert pcm_addr != 0
        assert mem.read32(obj) == DS_BUF_VTABLE
        assert mem.read32(obj + 8) == 512

    def test_secondary_buffer_parses_wfx_fields(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=512,
                                       sample_rate=22050, channels=1, bits=8)
        call_out = 0x340000
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetFormat", obj, [call_out, 18, 0])
        assert mem.read16(call_out + 2) == 1     # channels
        assert mem.read32(call_out + 4) == 22050  # sample rate
        assert mem.read16(call_out + 14) == 8     # bits per sample

    def test_secondary_buffer_null_wfx_uses_defaults(self, env):
        cpu, mem, state, stubs = env
        write_dsbufferdesc(mem, DESC_ADDR, flags=0, buf_bytes=512, wfx_ptr=0)
        com_call(stubs, cpu, mem, "dsound.dll", "DS::CreateSoundBuffer", DS_OBJ, [DESC_ADDR, BUF_A, 0])
        obj = mem.read32(BUF_A)
        out = 0x340000
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetFormat", obj, [out, 18, 0])
        assert mem.read16(out + 2) == 2       # default channels
        assert mem.read32(out + 4) == 44100   # default sample rate
        assert mem.read16(out + 14) == 16     # default bits

    def test_secondary_buffer_zero_wfx_fields_fall_back_to_defaults(self, env):
        cpu, mem, state, stubs = env
        # WAVEFORMATEX with channels=0, bits=0, sample_rate=0 (a driver reporting silence)
        write_wfx(mem, WFX_ADDR, sample_rate=0, channels=0, bits=0)
        write_dsbufferdesc(mem, DESC_ADDR, flags=0, buf_bytes=512, wfx_ptr=WFX_ADDR)
        com_call(stubs, cpu, mem, "dsound.dll", "DS::CreateSoundBuffer", DS_OBJ, [DESC_ADDR, BUF_A, 0])
        obj = mem.read32(BUF_A)
        out = 0x340000
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetFormat", obj, [out, 18, 0])
        assert mem.read16(out + 2) == 2
        assert mem.read32(out + 4) == 44100
        assert mem.read16(out + 14) == 16


# ── DS::GetCaps ────────────────────────────────────────────────────────────────

class TestDSGetCaps:

    def test_caller_specified_zero_uses_minimum_96(self, env):
        cpu, mem, state, stubs = env
        for off in range(0, 120, 4):
            mem.write32(BUF_A + off, 0xFFFFFFFF)
        mem.write32(BUF_A, 0)  # dwSize = 0, written last
        com_call(stubs, cpu, mem, "dsound.dll", "DS::GetCaps", DS_OBJ, [BUF_A])
        assert mem.read32(BUF_A) == 96
        assert mem.read32(BUF_A + 4) == 0x00000001
        assert mem.read32(BUF_A + 96) == 0xFFFFFFFF

    def test_null_pointer_does_not_crash(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::GetCaps", DS_OBJ, [0])  # must not raise
        assert cpu.regs[EAX] == DS_OK


# ── DS::DuplicateSoundBuffer ───────────────────────────────────────────────────

class TestDuplicateSoundBuffer:

    def test_missing_pointers_returns_invalidparam(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::DuplicateSoundBuffer", DS_OBJ, [0, BUF_B])
        assert cpu.regs[EAX] == DSERR_INVALIDPARAM

    def test_unknown_original_returns_invalidparam(self, env):
        cpu, mem, state, stubs = env
        # A COM-object-shaped struct whose idx field (offset 12) points nowhere real.
        mem.write32(BUF_A + 12, 0xFFFFFF)
        com_call(stubs, cpu, mem, "dsound.dll", "DS::DuplicateSoundBuffer", DS_OBJ, [BUF_A, BUF_B])
        assert cpu.regs[EAX] == DSERR_INVALIDPARAM

    def test_copies_pcm_bytes_into_new_buffer(self, env):
        cpu, mem, state, stubs = env
        orig = create_secondary_buffer(stubs, cpu, mem, buf_bytes=8)
        orig_pcm = mem.read32(orig + 4)
        for i, b in enumerate(b"ABCDEFGH"):
            mem.write8(orig_pcm + i, b)

        com_call(stubs, cpu, mem, "dsound.dll", "DS::DuplicateSoundBuffer", DS_OBJ, [orig, BUF_B])
        assert cpu.regs[EAX] == DS_OK
        dup = mem.read32(BUF_B)
        dup_pcm = mem.read32(dup + 4)
        assert dup_pcm != orig_pcm
        assert bytes(mem.read8(dup_pcm + i) for i in range(8)) == b"ABCDEFGH"
        assert mem.read32(dup) == DS_BUF_VTABLE
        assert mem.read32(dup + 8) == 8


# ── DS::SetCooperativeLevel / Compact / speaker config / Initialize ───────────

class TestDSFixedReturnMethods:

    def test_set_cooperative_level(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::SetCooperativeLevel", DS_OBJ, [0, 0])
        assert cpu.regs[EAX] == DS_OK

    def test_compact(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::Compact", DS_OBJ, [])
        assert cpu.regs[EAX] == DS_OK

    def test_get_speaker_config(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::GetSpeakerConfig", DS_OBJ, [0])
        assert cpu.regs[EAX] == DS_OK

    def test_set_speaker_config(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::SetSpeakerConfig", DS_OBJ, [0])
        assert cpu.regs[EAX] == DS_OK

    def test_initialize_always_already_initialized(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "DS::Initialize", DS_OBJ, [0])
        assert cpu.regs[EAX] == DSERR_ALREADYINITIALIZED


# ── Buf::QueryInterface / AddRef / Release ─────────────────────────────────────

class TestBufQueryInterface:

    def test_echoes_this_into_ppv(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::QueryInterface", obj, [0, BUF_B])
        assert mem.read32(BUF_B) == obj

    def test_null_ppv_does_not_crash(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::QueryInterface", obj, [0, 0])  # must not raise
        assert cpu.regs[EAX] == DS_OK


class TestBufAddRefRelease:

    def test_addref_returns_two(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::AddRef", 0x1234, [])
        assert cpu.regs[EAX] == 2

    def test_release_returns_one(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::Release", 0x1234, [])
        assert cpu.regs[EAX] == 1


# ── Buf::GetCaps ───────────────────────────────────────────────────────────────

class TestBufGetCaps:

    def test_writes_buffer_bytes(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=256)
        mem.write32(BUF_A, 0)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetCaps", obj, [BUF_A])
        assert mem.read32(BUF_A + 8) == 256

    def test_unknown_buffer_no_crash(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetCaps", 0, [BUF_A])  # must not raise
        assert cpu.regs[EAX] == DS_OK


# ── Buf::GetFormat ─────────────────────────────────────────────────────────────

class TestBufGetFormat:

    def test_insufficient_buffer_does_not_write(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem)
        mem.write32(BUF_A, 0xFFFFFFFF)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetFormat", obj, [BUF_A, 10, 0])  # < 18
        assert mem.read32(BUF_A) == 0xFFFFFFFF
        assert cpu.regs[EAX] == DS_OK

    def test_sufficient_buffer_writes_size_written(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, sample_rate=48000, channels=2, bits=16)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetFormat", obj, [BUF_A, 18, BUF_B])
        assert mem.read32(BUF_B) == 18
        assert mem.read16(BUF_A) == 1  # WAVE_FORMAT_PCM
        assert mem.read32(BUF_A + 8) == 48000 * 2 * 2  # avg bytes/sec
        assert mem.read16(BUF_A + 12) == 4             # block align


# ── Buf::GetStatus ─────────────────────────────────────────────────────────────

class TestBufGetStatus:

    def test_not_playing_not_looping(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetStatus", obj, [BUF_A])
        assert mem.read32(BUF_A) == 0

    def test_unknown_buffer_no_crash(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetStatus", 0, [BUF_A])  # must not raise
        assert cpu.regs[EAX] == DS_OK

    def test_playing_and_looping_flags_reported(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem)
        idx = mem.read32(obj + 12)
        _ds_buffers[idx].playing = True
        _ds_buffers[idx].looping = True
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetStatus", obj, [BUF_A])
        assert mem.read32(BUF_A) == DSBSTATUS_PLAYING | DSBSTATUS_LOOPING


# ── Buf::Stop / SetCurrentPosition / SetFormat ─────────────────────────────────

class TestBufStop:

    def test_unknown_buffer_no_crash(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::Stop", 0, [])  # must not raise
        assert cpu.regs[EAX] == DS_OK

    def test_returns_ds_ok(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::Stop", obj, [])
        assert cpu.regs[EAX] == DS_OK


class TestBufSetCurrentPosition:

    def test_wraps_modulo_buffer_size(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=100)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::SetCurrentPosition", obj, [150])
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetCurrentPosition", obj, [BUF_A, 0])
        assert mem.read32(BUF_A) == 50

    def test_unknown_buffer_no_crash(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::SetCurrentPosition", 0, [10])  # must not raise
        assert cpu.regs[EAX] == DS_OK


class TestBufSetFormat:

    def test_updates_buffer_format(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, sample_rate=44100, channels=2, bits=16)
        write_wfx(mem, WFX_ADDR, sample_rate=8000, channels=1, bits=8)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::SetFormat", obj, [WFX_ADDR])
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetFormat", obj, [BUF_A, 18, 0])
        assert mem.read32(BUF_A + 4) == 8000
        assert mem.read16(BUF_A + 2) == 1
        assert mem.read16(BUF_A + 14) == 8

    def test_unknown_buffer_no_crash(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::SetFormat", 0, [WFX_ADDR])  # must not raise
        assert cpu.regs[EAX] == DS_OK

    def test_null_wfx_pointer_no_crash(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::SetFormat", obj, [0])  # must not raise
        assert cpu.regs[EAX] == DS_OK


# ── Buf::GetCurrentPosition (pure math, no SDL playback needed) ───────────────

class TestBufGetCurrentPosition:

    def test_write_cursor_leads_play_cursor(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=100000,
                                       sample_rate=44100, channels=2, bits=16)
        # lead = int(44100 * 2 * 2 * 0.015) = 2646
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetCurrentPosition", obj, [BUF_A, BUF_B])
        assert mem.read32(BUF_A) == 0
        assert mem.read32(BUF_B) == 2646

    def test_write_cursor_wraps_modulo_buffer_size(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=2000,
                                       sample_rate=44100, channels=2, bits=16)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::SetCurrentPosition", obj, [0])
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetCurrentPosition", obj, [BUF_A, BUF_B])
        # lead = 2646, buf_size = 2000 -> write cursor wraps to 646
        assert mem.read32(BUF_B) == 646

    def test_unknown_buffer_no_crash(self, env):
        cpu, mem, state, stubs = env
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetCurrentPosition", 0, [BUF_A, BUF_B])  # must not raise
        assert cpu.regs[EAX] == DS_OK

    def test_null_play_cursor_pointer_skips_write(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=2000)
        mem.write32(BUF_B, 0xFFFFFFFF)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetCurrentPosition", obj, [0, BUF_B])
        assert mem.read32(BUF_B) != 0xFFFFFFFF  # write cursor was still written

    def test_null_write_cursor_pointer_skips_write(self, env):
        cpu, mem, state, stubs = env
        obj = create_secondary_buffer(stubs, cpu, mem, buf_bytes=2000)
        mem.write32(BUF_A, 0xFFFFFFFF)
        com_call(stubs, cpu, mem, "dsound.dll", "Buf::GetCurrentPosition", obj, [BUF_A, 0])
        assert mem.read32(BUF_A) == 0  # play cursor was still written


# ── Buf:: fixed-return methods ─────────────────────────────────────────────────

BUF_FIXED_RETURN_CASES = [
    ("Buf::GetVolume", 4, DS_OK),
    ("Buf::GetPan", 4, DS_OK),
    ("Buf::GetFrequency", 4, DS_OK),
    ("Buf::Initialize", 8, DSERR_ALREADYINITIALIZED),
    ("Buf::SetVolume", 4, DS_OK),
    ("Buf::SetPan", 4, DS_OK),
    ("Buf::SetFrequency", 4, DS_OK),
    ("Buf::Unlock", 16, DS_OK),
    ("Buf::Restore", 0, DS_OK),
]


class TestBufFixedReturnMethods:

    @pytest.mark.parametrize("name,arg_bytes,expected", BUF_FIXED_RETURN_CASES)
    def test_returns_expected_code(self, env, name, arg_bytes, expected):
        cpu, mem, state, stubs = env
        args = [0] * (arg_bytes // 4)
        com_call(stubs, cpu, mem, "dsound.dll", name, 0x1234, args)
        assert cpu.regs[EAX] == expected


# ── DirectSoundCreate / aliases ────────────────────────────────────────────────

class TestDirectSoundCreate:

    @pytest.mark.parametrize("name", ["DirectSoundCreate", "Ordinal #1", "DirectSoundCreate8"])
    def test_writes_ds_obj(self, env, name):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "dsound.dll", name, [0, BUF_A, 0])
        assert mem.read32(BUF_A) == DS_OBJ
        assert cpu.regs[EAX] == DS_OK

    def test_null_out_pointer_does_not_crash(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "dsound.dll", "DirectSoundCreate", [0, 0, 0])  # must not raise
        assert cpu.regs[EAX] == DS_OK

    def test_stdcall_cleanup(self, env):
        cpu, mem, state, stubs = env
        call(stubs, cpu, mem, "dsound.dll", "DirectSoundCreate", [0, BUF_A, 0])
        assert cpu.regs[ESP] == STACK + 12


# ── Setup smoke test ────────────────────────────────────────────────────────────

class TestVtableSetup:

    def test_ds_obj_points_to_ds_vtable(self, env):
        cpu, mem, state, stubs = env
        assert mem.read32(DS_OBJ) == DS_VTABLE
