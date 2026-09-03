"""Tests for dsound.dll's SDL-audio-backed Buf::Play path and the pure
_mix_into mixing function -- the third and last part of the dsound split
(see test_dsound_buffer_lifecycle.py and test_dsound_buffer_lock_unlock.py
for the rest of the file).

SDL_VIDEODRIVER/SDL_AUDIODRIVER=dummy must be set before any sdl2 import
(established convention, see test_dialog_click_integration.py). Empirically
confirmed in this sandbox: the dummy audio driver only opens a device once
SDL_INIT_AUDIO has actually run (SDL_OpenAudioDevice fails with "Audio
subsystem is not initialized" otherwise) -- a module-scoped fixture below
calls SDL_Init(SDL_INIT_AUDIO)/SDL_Quit around this file's tests.

Buf::Play tests drive the guard logic (_sdl_audio_dev[0]) through the real
handler chain but mock _open_sdl_audio itself, since _sdl_audio_dev is a
module-level global with no per-test reset hook shared across the whole
session -- mocking plus an explicit reset makes the guard assertions
order-independent. _open_sdl_audio's own internals get real (unmocked)
coverage from TestOpenSdlAudio below.
"""
from __future__ import annotations

import array
import os
import struct
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pytest

import tew.api.dsound_handlers as dsh
from tew.api._state import CRTState
from tew.api.dsound_handlers import (
    register_dsound_handlers,
    _mix_into,
    _open_sdl_audio,
    _DSBuffer,
    DS_OBJ,
    DS_OK,
    DSBCAPS_PRIMARYBUFFER,
    DSBPLAY_LOOPING,
)
from tew.hardware.memory import Memory
from tew.hardware.cpu_zig import EAX, ESP
from tew import logger as logger_module


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
DESC_ADDR = 0x320000


@pytest.fixture(scope="module", autouse=True)
def _sdl_audio_subsystem():
    from sdl2 import SDL_Init, SDL_Quit, SDL_INIT_AUDIO
    rc = SDL_Init(SDL_INIT_AUDIO)
    assert rc == 0, "SDL_Init(SDL_INIT_AUDIO) failed even with the dummy driver"
    yield
    SDL_Quit()


@pytest.fixture
def env():
    mem   = Memory(MEM_SIZE)
    state = CRTState()
    stubs = _StubHandlers()
    register_dsound_handlers(stubs, mem, state)
    cpu = _FakeCPU()
    return cpu, mem, state, stubs


@pytest.fixture
def captured_logs():
    lines: list[str] = []
    logger_module.set_emit_hook(lambda level, line: lines.append(line))
    yield lines
    logger_module.set_emit_hook(None)


def com_call(stubs, cpu, mem, dll, name, this_ptr, args):
    cpu.regs[ESP] = STACK
    mem.write32(STACK, 0xDEAD)
    mem.write32(STACK + 4, this_ptr)
    for i, val in enumerate(args):
        mem.write32(STACK + 8 + i * 4, val)
    stubs.get(dll, name)(cpu)


def write_dsbufferdesc(mem, addr, flags=0, buf_bytes=0, wfx_ptr=0):
    mem.write32(addr, 0)
    mem.write32(addr + 4, flags)
    mem.write32(addr + 8, buf_bytes)
    mem.write32(addr + 16, wfx_ptr)


def create_secondary_buffer(stubs, cpu, mem, buf_bytes=1024):
    write_dsbufferdesc(mem, DESC_ADDR, flags=0, buf_bytes=buf_bytes, wfx_ptr=0)
    com_call(stubs, cpu, mem, "dsound.dll", "DS::CreateSoundBuffer", DS_OBJ, [DESC_ADDR, BUF_A, 0])
    return mem.read32(BUF_A)


def create_primary_buffer(stubs, cpu, mem):
    write_dsbufferdesc(mem, DESC_ADDR, flags=DSBCAPS_PRIMARYBUFFER)
    com_call(stubs, cpu, mem, "dsound.dll", "DS::CreateSoundBuffer", DS_OBJ, [DESC_ADDR, BUF_A, 0])
    return mem.read32(BUF_A)


# ── _open_sdl_audio (real, unmocked -- exercises actual SDL calls) ────────────

class TestOpenSdlAudio:

    def test_dummy_driver_opens_a_device(self, captured_logs):
        mem_buf = bytearray(4096)
        dev = _open_sdl_audio(mem_buf, 44100, 2, 16)
        assert dev > 0
        assert any("SDL audio opened" in line for line in captured_logs)

    def test_open_device_failure_returns_zero(self, captured_logs):
        mem_buf = bytearray(4096)
        with patch("sdl2.SDL_OpenAudioDevice", return_value=0):
            dev = _open_sdl_audio(mem_buf, 44100, 2, 16)
        assert dev == 0
        assert any("SDL_OpenAudioDevice failed" in line for line in captured_logs)

    def test_exception_during_init_returns_zero(self, captured_logs):
        mem_buf = bytearray(4096)
        with patch("sdl2.SDL_AudioSpec", side_effect=RuntimeError("boom")):
            dev = _open_sdl_audio(mem_buf, 44100, 2, 16)
        assert dev == 0
        assert any("SDL audio init failed" in line for line in captured_logs)


# ── Buf::Play guard logic (mocked _open_sdl_audio, explicit global reset) ─────

class TestBufPlay:

    def test_primary_buffer_does_not_open_sdl(self, env):
        cpu, mem, state, stubs = env
        with patch.object(dsh, "_open_sdl_audio", return_value=999) as mock_open:
            dsh._sdl_audio_dev[0] = 0
            obj = create_primary_buffer(stubs, cpu, mem)
            com_call(stubs, cpu, mem, "dsound.dll", "Buf::Play", obj, [0, 0, 0])
            mock_open.assert_not_called()
            assert cpu.regs[EAX] == DS_OK

    def test_first_play_opens_sdl_audio(self, env):
        cpu, mem, state, stubs = env
        with patch.object(dsh, "_open_sdl_audio", return_value=999) as mock_open:
            dsh._sdl_audio_dev[0] = 0
            obj = create_secondary_buffer(stubs, cpu, mem)
            com_call(stubs, cpu, mem, "dsound.dll", "Buf::Play", obj, [0, 0, 0])
            mock_open.assert_called_once()
            assert dsh._sdl_audio_dev[0] == 999

    def test_first_play_sets_playing_true(self, env):
        cpu, mem, state, stubs = env
        with patch.object(dsh, "_open_sdl_audio", return_value=999):
            dsh._sdl_audio_dev[0] = 0
            obj = create_secondary_buffer(stubs, cpu, mem)
            idx = mem.read32(obj + 12)
            com_call(stubs, cpu, mem, "dsound.dll", "Buf::Play", obj, [0, 0, 0])
            assert dsh._ds_buffers[idx].playing is True

    def test_looping_flag_set_from_dsbplay_looping(self, env):
        cpu, mem, state, stubs = env
        with patch.object(dsh, "_open_sdl_audio", return_value=999):
            dsh._sdl_audio_dev[0] = 0
            obj = create_secondary_buffer(stubs, cpu, mem)
            idx = mem.read32(obj + 12)
            com_call(stubs, cpu, mem, "dsound.dll", "Buf::Play", obj, [0, 0, DSBPLAY_LOOPING])
            assert dsh._ds_buffers[idx].looping is True

    def test_no_looping_flag_leaves_looping_false(self, env):
        cpu, mem, state, stubs = env
        with patch.object(dsh, "_open_sdl_audio", return_value=999):
            dsh._sdl_audio_dev[0] = 0
            obj = create_secondary_buffer(stubs, cpu, mem)
            idx = mem.read32(obj + 12)
            com_call(stubs, cpu, mem, "dsound.dll", "Buf::Play", obj, [0, 0, 0])
            assert dsh._ds_buffers[idx].looping is False

    def test_second_play_on_different_buffer_does_not_reopen(self, env):
        cpu, mem, state, stubs = env
        with patch.object(dsh, "_open_sdl_audio", return_value=999) as mock_open:
            dsh._sdl_audio_dev[0] = 0
            obj1 = create_secondary_buffer(stubs, cpu, mem)
            com_call(stubs, cpu, mem, "dsound.dll", "Buf::Play", obj1, [0, 0, 0])
            obj2 = create_secondary_buffer(stubs, cpu, mem)
            com_call(stubs, cpu, mem, "dsound.dll", "Buf::Play", obj2, [0, 0, 0])
            mock_open.assert_called_once()

    def test_unknown_buffer_returns_ok_without_opening_sdl(self, env):
        cpu, mem, state, stubs = env
        with patch.object(dsh, "_open_sdl_audio", return_value=999) as mock_open:
            dsh._sdl_audio_dev[0] = 0
            com_call(stubs, cpu, mem, "dsound.dll", "Buf::Play", 0, [0, 0, 0])
            mock_open.assert_not_called()
            assert cpu.regs[EAX] == DS_OK


# ── _mix_into (pure function, no SDL device needed) ────────────────────────────

def make_buffer(**kwargs):
    defaults = dict(pcm_addr=0, buf_size=8, sample_rate=44100, channels=1, bits_per_sample=16)
    defaults.update(kwargs)
    return _DSBuffer(**defaults)


class TestMixInto16Bit:

    def test_clips_positive_overflow(self):
        buf = make_buffer(buf_size=4, bits_per_sample=16)
        mem_buf = bytearray(4)
        mem_buf[0:2] = struct.pack("<h", 20000)
        out = array.array("h", [20000])
        _mix_into(buf, mem_buf, out, length=2)
        assert out[0] == 32767

    def test_clips_negative_overflow(self):
        buf = make_buffer(buf_size=4, bits_per_sample=16)
        mem_buf = bytearray(4)
        mem_buf[0:2] = struct.pack("<h", -20000)
        out = array.array("h", [-20000])
        _mix_into(buf, mem_buf, out, length=2)
        assert out[0] == -32768

    def test_no_clip_within_range(self):
        buf = make_buffer(buf_size=4, bits_per_sample=16)
        mem_buf = bytearray(4)
        mem_buf[0:2] = struct.pack("<h", 100)
        out = array.array("h", [200])
        _mix_into(buf, mem_buf, out, length=2)
        assert out[0] == 300

    def test_advances_play_cursor(self):
        buf = make_buffer(buf_size=8, bits_per_sample=16, play_cursor=0)
        mem_buf = bytearray(8)
        out = array.array("h", [0, 0])
        _mix_into(buf, mem_buf, out, length=4)
        assert buf.play_cursor == 4

    def test_odd_length_chunk_swallows_exception_and_leaves_output_untouched(self):
        buf = make_buffer(buf_size=100, bits_per_sample=16, play_cursor=0)
        mem_buf = bytearray(100)
        out = array.array("h", [111, 222])
        _mix_into(buf, mem_buf, out, length=3)  # odd byte count -- frombytes raises
        assert list(out) == [111, 222]
        assert buf.play_cursor == 0


class TestMixInto8Bit:

    def test_converts_unsigned_byte_to_signed_16bit(self):
        buf = make_buffer(buf_size=4, bits_per_sample=8)
        mem_buf = bytearray([255, 0, 128, 64])
        out = array.array("h", [0, 0, 0, 0])
        _mix_into(buf, mem_buf, out, length=4)
        assert list(out) == [(255 - 128) * 256, (0 - 128) * 256, 0, (64 - 128) * 256]


class TestMixIntoLoopingAndExhaustion:

    def test_looping_wraps_cursor_after_reaching_end(self):
        buf = make_buffer(buf_size=8, bits_per_sample=16, play_cursor=6, looping=True)
        mem_buf = bytearray(8)
        out = array.array("h", [0, 0, 0, 0])
        _mix_into(buf, mem_buf, out, length=8)
        assert buf.play_cursor == 0  # 6 + 2 (remaining) = 8 -> wraps to 0

    def test_looping_cursor_already_at_buffer_size_wraps_before_copy(self):
        buf = make_buffer(buf_size=8, bits_per_sample=16, play_cursor=8, looping=True)
        mem_buf = bytearray(8)
        out = array.array("h", [0, 0, 0, 0])
        _mix_into(buf, mem_buf, out, length=4)
        assert buf.play_cursor == 4  # wrapped to 0, then advanced by copy_bytes

    def test_non_looping_exhausted_stops_playing(self):
        buf = make_buffer(buf_size=8, bits_per_sample=16, play_cursor=8, looping=False, playing=True)
        mem_buf = bytearray(8)
        out = array.array("h", [111, 222, 333, 444])
        _mix_into(buf, mem_buf, out, length=8)
        assert buf.playing is False
        assert list(out) == [111, 222, 333, 444]  # untouched -- exhausted before any copy
