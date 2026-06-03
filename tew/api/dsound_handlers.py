"""dsound.dll handler registrations.

Real PCM audio via SDL2 audio callback.  IDirectSound + IDirectSoundBuffer COM
stubs sufficient for DirectSound-based game audio.

Fixed-data region addresses (immediately after DInput vtables at 0x00220318):
    DS_VTABLE     = 0x00220320  IDirectSound      11 slots × 4 =  44 bytes
    DS_OBJ        = 0x00220350  IDirectSound singleton (4 bytes)
    DS_BUF_VTABLE = 0x00220360  IDirectSoundBuffer 21 slots × 4 =  84 bytes

Buffer COM object layout (16 bytes, bump-allocated from D3D8 heap):
    [0]  vtable ptr   DS_BUF_VTABLE
    [4]  pcm_addr     address of PCM data in emulator memory
    [8]  buf_size     total circular buffer size in bytes
    [12] buf_index    key into module-level _ds_buffers dict

SDL audio callback reads live from emulator memory.  The game writes PCM via
Lock/Unlock; no copy is needed — same bytearray the callback slices.
"""

from __future__ import annotations

import array as _array
import ctypes
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tew.hardware.cpu import CPU
    from tew.hardware.memory import Memory

from tew.hardware.cpu import EAX, ESP
from tew.api.win32_handlers import Win32Handlers
from tew.api.d3d8._helpers import _com_stub, _heap_alloc, _set_eax
from tew.api._state import CRTState
from tew.logger import logger

# ── Fixed COM addresses ────────────────────────────────────────────────────────
DS_VTABLE     = 0x00220320   # IDirectSound vtable       (11 × 4 = 44 bytes)
DS_OBJ        = 0x00220350   # IDirectSound singleton    (4 bytes)
DS_BUF_VTABLE = 0x00220360   # IDirectSoundBuffer vtable (21 × 4 = 84 bytes)

# ── Status codes ──────────────────────────────────────────────────────────────
DS_OK                    = 0x00000000
DSERR_NODRIVER           = 0x88780078
DSERR_INVALIDPARAM       = 0x80070057
DSERR_ALREADYINITIALIZED = 0x88780082

# ── DSBUFFERDESC / buffer flags ───────────────────────────────────────────────
DSBCAPS_PRIMARYBUFFER   = 0x00000001
DSBPLAY_LOOPING         = 0x00000001
DSBLOCK_ENTIREBUFFER    = 0x00000002

# DSBSTATUS
DSBSTATUS_PLAYING       = 0x00000001
DSBSTATUS_BUFFERLOST    = 0x00000002
DSBSTATUS_LOOPING       = 0x00000004


# ── Per-buffer Python state ───────────────────────────────────────────────────

@dataclass
class _DSBuffer:
    pcm_addr: int
    buf_size: int
    sample_rate: int
    channels: int
    bits_per_sample: int
    is_primary: bool = False
    playing: bool = False
    looping: bool = False
    play_cursor: int = 0   # advanced by SDL callback


# Module-level state — shared between game thread and SDL audio thread.
_ds_buffers: dict[int, _DSBuffer]   = {}
_ds_buf_lock:   threading.Lock       = threading.Lock()
_next_buf_idx:  list[int]            = [0]
_sdl_audio_dev: list[int]            = [0]    # 0 = not opened
_callback_ref:  list                 = [None]  # keep CFUNCTYPE alive


# ── SDL audio helpers ─────────────────────────────────────────────────────────

def _mix_into(buf: _DSBuffer, mem_buf: bytearray,
              out: "_array.ArrayType[int]", length: int) -> None:
    """Mix one DSBuffer's PCM data into the S16 output array, advancing play_cursor."""
    remaining = buf.buf_size - buf.play_cursor
    if remaining <= 0:
        if not buf.looping:
            buf.playing = False
            return
        buf.play_cursor = 0
        remaining = buf.buf_size

    copy_bytes = min(length, remaining)
    src_start  = buf.pcm_addr + buf.play_cursor
    chunk      = mem_buf[src_start: src_start + copy_bytes]

    if buf.bits_per_sample == 16:
        src = _array.array('h')
        try:
            src.frombytes(bytes(chunk))
        except Exception:
            return
        n = min(len(src), len(out))
        for i in range(n):
            mixed = out[i] + src[i]
            out[i] = 32767 if mixed > 32767 else (-32768 if mixed < -32768 else mixed)
    else:
        # 8-bit unsigned → convert to S16
        n = min(len(chunk), len(out))
        for i in range(n):
            s = (chunk[i] - 128) * 256
            mixed = out[i] + s
            out[i] = 32767 if mixed > 32767 else (-32768 if mixed < -32768 else mixed)

    buf.play_cursor += copy_bytes
    if buf.looping and buf.play_cursor >= buf.buf_size:
        buf.play_cursor %= buf.buf_size


def _open_sdl_audio(mem_buf: bytearray,
                    sample_rate: int, channels: int, bits: int) -> int:
    """Open SDL2 audio device.  Returns device ID (> 0) on success."""
    try:
        from sdl2 import (
            SDL_AudioSpec, SDL_AudioCallback,
            SDL_OpenAudioDevice, SDL_PauseAudioDevice,
            AUDIO_S16SYS, AUDIO_U8,
        )

        fmt = AUDIO_S16SYS if bits == 16 else AUDIO_U8

        def _callback(userdata: int,
                       stream: "ctypes.POINTER[ctypes.c_uint8]",
                       length: int) -> None:
            stream_addr = ctypes.cast(stream, ctypes.c_void_p).value
            ctypes.memset(stream_addr, 0, length)
            with _ds_buf_lock:
                active = [b for b in _ds_buffers.values()
                          if b.playing and not b.is_primary]
            if not active:
                return
            out = _array.array('h', b'\x00' * length)
            for buf in active:
                _mix_into(buf, mem_buf, out, length)
            ctypes.memmove(stream_addr, out.tobytes(), length)

        cb = SDL_AudioCallback(_callback)
        _callback_ref[0] = cb   # prevent GC

        spec = SDL_AudioSpec()
        spec.freq     = sample_rate
        spec.format   = fmt
        spec.channels = channels
        spec.samples  = 2048
        spec.callback = cb
        spec.userdata = None

        dev_id = int(SDL_OpenAudioDevice(None, 0, spec, None, 0))
        if dev_id == 0:
            logger.warn("handlers", "DirectSound: SDL_OpenAudioDevice failed")
            return 0
        SDL_PauseAudioDevice(dev_id, 0)
        logger.info("handlers",
            f"DirectSound: SDL audio opened {sample_rate}Hz/{channels}ch/{bits}bit "
            f"dev={dev_id}")
        return dev_id
    except Exception as exc:
        logger.warn("handlers", f"DirectSound: SDL audio init failed: {exc}")
        return 0


# ── Registration ──────────────────────────────────────────────────────────────

def register_dsound_handlers(
    stubs: "Win32Handlers",
    memory: "Memory",
    state: "CRTState",
) -> None:
    """Register all DirectSound COM stubs and write vtable pointers into memory."""

    mem_buf = memory._buffer   # direct bytearray reference for fast callback reads

    # ── WAVEFORMATEX reader helper ─────────────────────────────────────────

    def _read_wfx(ptr: int) -> tuple[int, int, int]:
        """Return (sample_rate, channels, bits_per_sample) from WAVEFORMATEX."""
        if not ptr:
            return 44100, 2, 16
        channels       = memory.read16((ptr + 2)  & 0xFFFFFFFF)
        sample_rate    = memory.read32((ptr + 4)  & 0xFFFFFFFF)
        bits_per_sample= memory.read16((ptr + 14) & 0xFFFFFFFF)
        return sample_rate or 44100, channels or 2, bits_per_sample or 16

    # ── IDirectSound vtable ────────────────────────────────────────────────

    def _ds_query_interface(cpu: "CPU", mem: "Memory") -> None:
        ppv = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if ppv:
            mem.write32(ppv, DS_OBJ)
        cpu.regs[EAX] = DS_OK

    def _ds_create_sound_buffer(cpu: "CPU", mem: "Memory") -> None:
        lp_desc    = mem.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp_lp_dsb  = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if not lp_desc or not lp_lp_dsb:
            cpu.regs[EAX] = DSERR_INVALIDPARAM
            return

        dw_flags   = mem.read32((lp_desc + 4)  & 0xFFFFFFFF)
        buf_bytes  = mem.read32((lp_desc + 8)  & 0xFFFFFFFF)
        lp_wfx_ptr = mem.read32((lp_desc + 16) & 0xFFFFFFFF)
        is_primary = bool(dw_flags & DSBCAPS_PRIMARYBUFFER)

        if is_primary:
            sr, ch, bps = _read_wfx(lp_wfx_ptr) if lp_wfx_ptr else (44100, 2, 16)
            buf_bytes = buf_bytes or 0
            pcm_addr  = 0
        else:
            sr, ch, bps = _read_wfx(lp_wfx_ptr)
            if not buf_bytes:
                cpu.regs[EAX] = DSERR_INVALIDPARAM
                return
            pcm_addr = state.simple_alloc(buf_bytes)

        idx = _next_buf_idx[0]
        _next_buf_idx[0] += 1

        with _ds_buf_lock:
            _ds_buffers[idx] = _DSBuffer(
                pcm_addr=pcm_addr,
                buf_size=buf_bytes,
                sample_rate=sr,
                channels=ch,
                bits_per_sample=bps,
                is_primary=is_primary,
            )

        # Allocate COM object in emulator memory (16 bytes)
        obj = _heap_alloc(16)
        mem.write32(obj,      DS_BUF_VTABLE)
        mem.write32(obj + 4,  pcm_addr)
        mem.write32(obj + 8,  buf_bytes)
        mem.write32(obj + 12, idx)
        mem.write32(lp_lp_dsb, obj)

        logger.info("handlers",
            f"IDirectSound::CreateSoundBuffer({'primary' if is_primary else 'secondary'} "
            f"{sr}Hz/{ch}ch/{bps}bit buf={buf_bytes}B) -> obj=0x{obj:08x}")
        cpu.regs[EAX] = DS_OK

    def _ds_get_caps(cpu: "CPU", mem: "Memory") -> None:
        lp = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if lp:
            dw_size = mem.read32(lp & 0xFFFFFFFF)
            size = max(dw_size, 96) if dw_size else 96
            for off in range(0, size, 4):
                mem.write32((lp + off) & 0xFFFFFFFF, 0)
            mem.write32(lp, size)                 # dwSize
            mem.write32(lp + 4, 0x00000001)       # dwFlags: DSCAPS_PRIMARYMONO
        cpu.regs[EAX] = DS_OK

    def _ds_set_cooperative_level(cpu: "CPU", mem: "Memory") -> None:
        cpu.regs[EAX] = DS_OK

    def _ds_duplicate_sound_buffer(cpu: "CPU", mem: "Memory") -> None:
        lp_orig     = mem.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        lp_lp_dup   = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if not lp_orig or not lp_lp_dup:
            cpu.regs[EAX] = DSERR_INVALIDPARAM
            return
        orig_idx = mem.read32((lp_orig + 12) & 0xFFFFFFFF)
        with _ds_buf_lock:
            orig = _ds_buffers.get(orig_idx)
        if orig is None:
            cpu.regs[EAX] = DSERR_INVALIDPARAM
            return
        # Allocate new PCM buffer and copy
        pcm_addr = state.simple_alloc(orig.buf_size)
        mem_buf[pcm_addr: pcm_addr + orig.buf_size] = \
            mem_buf[orig.pcm_addr: orig.pcm_addr + orig.buf_size]
        idx = _next_buf_idx[0]
        _next_buf_idx[0] += 1
        with _ds_buf_lock:
            _ds_buffers[idx] = _DSBuffer(
                pcm_addr=pcm_addr, buf_size=orig.buf_size,
                sample_rate=orig.sample_rate, channels=orig.channels,
                bits_per_sample=orig.bits_per_sample,
            )
        obj = _heap_alloc(16)
        mem.write32(obj,      DS_BUF_VTABLE)
        mem.write32(obj + 4,  pcm_addr)
        mem.write32(obj + 8,  orig.buf_size)
        mem.write32(obj + 12, idx)
        mem.write32(lp_lp_dup, obj)
        cpu.regs[EAX] = DS_OK

    ds_vtable = [
        # [0] QueryInterface(REFIID, void**)
        _com_stub(stubs, "dsound.dll", "DS::QueryInterface",
                  _ds_query_interface, 8, memory, DS_OBJ),
        # [1] AddRef()
        _com_stub(stubs, "dsound.dll", "DS::AddRef",
                  lambda cpu, mem: _set_eax(cpu, 2), 0, memory, DS_OBJ),
        # [2] Release()
        _com_stub(stubs, "dsound.dll", "DS::Release",
                  lambda cpu, mem: _set_eax(cpu, 1), 0, memory, DS_OBJ),
        # [3] CreateSoundBuffer(lpDSBufferDesc, lplpDSBuffer, pUnkOuter)
        _com_stub(stubs, "dsound.dll", "DS::CreateSoundBuffer",
                  _ds_create_sound_buffer, 12, memory, DS_OBJ),
        # [4] GetCaps(lpDSCaps)
        _com_stub(stubs, "dsound.dll", "DS::GetCaps",
                  _ds_get_caps, 4, memory, DS_OBJ),
        # [5] DuplicateSoundBuffer(lpDsbOriginal, lplpDsbDuplicate)
        _com_stub(stubs, "dsound.dll", "DS::DuplicateSoundBuffer",
                  _ds_duplicate_sound_buffer, 8, memory, DS_OBJ),
        # [6] SetCooperativeLevel(hwnd, dwLevel)
        _com_stub(stubs, "dsound.dll", "DS::SetCooperativeLevel",
                  _ds_set_cooperative_level, 8, memory, DS_OBJ),
        # [7] Compact()
        _com_stub(stubs, "dsound.dll", "DS::Compact",
                  lambda cpu, mem: _set_eax(cpu, DS_OK), 0, memory, DS_OBJ),
        # [8] GetSpeakerConfig(lpdwSpeakerConfig)
        _com_stub(stubs, "dsound.dll", "DS::GetSpeakerConfig",
                  lambda cpu, mem: _set_eax(cpu, DS_OK), 4, memory, DS_OBJ),
        # [9] SetSpeakerConfig(dwSpeakerConfig)
        _com_stub(stubs, "dsound.dll", "DS::SetSpeakerConfig",
                  lambda cpu, mem: _set_eax(cpu, DS_OK), 4, memory, DS_OBJ),
        # [10] Initialize(lpcGuid)
        _com_stub(stubs, "dsound.dll", "DS::Initialize",
                  lambda cpu, mem: _set_eax(cpu, DSERR_ALREADYINITIALIZED), 4, memory, DS_OBJ),
    ]
    for i, addr in enumerate(ds_vtable):
        memory.write32(DS_VTABLE + i * 4, addr)
    memory.write32(DS_OBJ, DS_VTABLE)

    # ── IDirectSoundBuffer vtable ──────────────────────────────────────────

    def _buf_this(cpu: "CPU", mem: "Memory") -> "_DSBuffer | None":
        """Read `this` from ESP+4, look up buffer state.  Returns None if invalid."""
        obj = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        if not obj:
            return None
        idx = mem.read32((obj + 12) & 0xFFFFFFFF)
        with _ds_buf_lock:
            return _ds_buffers.get(idx)

    def _buf_query_interface(cpu: "CPU", mem: "Memory") -> None:
        this = mem.read32((cpu.regs[ESP] + 4) & 0xFFFFFFFF)
        ppv  = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        if ppv:
            mem.write32(ppv, this)
        cpu.regs[EAX] = DS_OK

    def _buf_get_caps(cpu: "CPU", mem: "Memory") -> None:
        lp = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        buf = _buf_this(cpu, mem)
        if lp and buf:
            dw_size = mem.read32(lp & 0xFFFFFFFF)
            size = max(dw_size, 36) if dw_size else 36
            for off in range(0, size, 4):
                mem.write32((lp + off) & 0xFFFFFFFF, 0)
            mem.write32(lp,       size)
            mem.write32(lp + 8,   buf.buf_size)   # dwBufferBytes
        cpu.regs[EAX] = DS_OK

    def _buf_get_current_position(cpu: "CPU", mem: "Memory") -> None:
        lp_play  = mem.read32((cpu.regs[ESP] + 8)  & 0xFFFFFFFF)
        lp_write = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        buf = _buf_this(cpu, mem)
        if buf:
            with _ds_buf_lock:
                play = buf.play_cursor
            # Write cursor leads play cursor by ~15ms worth of bytes
            bps       = buf.bits_per_sample // 8
            lead      = int(buf.sample_rate * buf.channels * bps * 0.015)
            write_cur = (play + lead) % buf.buf_size if buf.buf_size else 0
            if lp_play:
                mem.write32(lp_play,  play  & 0xFFFFFFFF)
            if lp_write:
                mem.write32(lp_write, write_cur & 0xFFFFFFFF)
        cpu.regs[EAX] = DS_OK

    def _buf_get_format(cpu: "CPU", mem: "Memory") -> None:
        lp_wfx       = mem.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        dw_allocated = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_written   = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        buf = _buf_this(cpu, mem)
        if lp_wfx and buf and dw_allocated >= 18:
            bps = buf.bits_per_sample
            ch  = buf.channels
            sr  = buf.sample_rate
            mem.write16((lp_wfx +  0) & 0xFFFFFFFF, 1)          # WAVE_FORMAT_PCM
            mem.write16((lp_wfx +  2) & 0xFFFFFFFF, ch)
            mem.write32((lp_wfx +  4) & 0xFFFFFFFF, sr)
            mem.write32((lp_wfx +  8) & 0xFFFFFFFF, sr * ch * (bps // 8))
            mem.write16((lp_wfx + 12) & 0xFFFFFFFF, ch * (bps // 8))
            mem.write16((lp_wfx + 14) & 0xFFFFFFFF, bps)
            mem.write16((lp_wfx + 16) & 0xFFFFFFFF, 0)
            if lp_written:
                mem.write32(lp_written, 18)
        cpu.regs[EAX] = DS_OK

    def _buf_get_status(cpu: "CPU", mem: "Memory") -> None:
        lp = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        buf = _buf_this(cpu, mem)
        if lp and buf:
            status = 0
            if buf.playing:
                status |= DSBSTATUS_PLAYING
            if buf.looping:
                status |= DSBSTATUS_LOOPING
            mem.write32(lp, status)
        cpu.regs[EAX] = DS_OK

    def _buf_lock(cpu: "CPU", mem: "Memory") -> None:
        obj           = mem.read32((cpu.regs[ESP] +  4) & 0xFFFFFFFF)
        dw_cursor     = mem.read32((cpu.regs[ESP] +  8) & 0xFFFFFFFF)
        dw_bytes      = mem.read32((cpu.regs[ESP] + 12) & 0xFFFFFFFF)
        lp_ptr1       = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        lp_bytes1     = mem.read32((cpu.regs[ESP] + 20) & 0xFFFFFFFF)
        lp_ptr2       = mem.read32((cpu.regs[ESP] + 24) & 0xFFFFFFFF)
        lp_bytes2     = mem.read32((cpu.regs[ESP] + 28) & 0xFFFFFFFF)
        dw_flags      = mem.read32((cpu.regs[ESP] + 32) & 0xFFFFFFFF)

        if not obj:
            cpu.regs[EAX] = DSERR_INVALIDPARAM
            return
        idx = mem.read32((obj + 12) & 0xFFFFFFFF)
        with _ds_buf_lock:
            buf = _ds_buffers.get(idx)
        if buf is None or buf.is_primary:
            if lp_ptr1:   mem.write32(lp_ptr1,   0)
            if lp_bytes1: mem.write32(lp_bytes1, 0)
            if lp_ptr2:   mem.write32(lp_ptr2,   0)
            if lp_bytes2: mem.write32(lp_bytes2, 0)
            cpu.regs[EAX] = DS_OK
            return

        if dw_flags & DSBLOCK_ENTIREBUFFER:
            dw_cursor = 0
            dw_bytes  = buf.buf_size

        bs = buf.buf_size
        start  = dw_cursor % bs
        end    = start + dw_bytes

        if end <= bs:
            bytes1 = dw_bytes
            bytes2 = 0
        else:
            bytes1 = bs - start
            bytes2 = dw_bytes - bytes1

        if lp_ptr1:   mem.write32(lp_ptr1,   (buf.pcm_addr + start) & 0xFFFFFFFF)
        if lp_bytes1: mem.write32(lp_bytes1, bytes1)
        if lp_ptr2:   mem.write32(lp_ptr2,   buf.pcm_addr if bytes2 else 0)
        if lp_bytes2: mem.write32(lp_bytes2, bytes2)
        cpu.regs[EAX] = DS_OK

    def _buf_play(cpu: "CPU", mem: "Memory") -> None:
        dw_flags = mem.read32((cpu.regs[ESP] + 16) & 0xFFFFFFFF)
        buf = _buf_this(cpu, mem)
        if buf is None or buf.is_primary:
            cpu.regs[EAX] = DS_OK
            return

        # Open SDL audio on first play if not yet open
        if _sdl_audio_dev[0] == 0:
            dev = _open_sdl_audio(mem_buf, buf.sample_rate, buf.channels, buf.bits_per_sample)
            _sdl_audio_dev[0] = dev

        with _ds_buf_lock:
            buf.looping = bool(dw_flags & DSBPLAY_LOOPING)
            buf.playing = True
        cpu.regs[EAX] = DS_OK

    def _buf_set_current_position(cpu: "CPU", mem: "Memory") -> None:
        dw_pos = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        buf = _buf_this(cpu, mem)
        if buf:
            with _ds_buf_lock:
                buf.play_cursor = dw_pos % buf.buf_size if buf.buf_size else 0
        cpu.regs[EAX] = DS_OK

    def _buf_set_format(cpu: "CPU", mem: "Memory") -> None:
        lp_wfx = mem.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        buf = _buf_this(cpu, mem)
        if buf and lp_wfx:
            sr, ch, bps = _read_wfx(lp_wfx)
            with _ds_buf_lock:
                buf.sample_rate     = sr
                buf.channels        = ch
                buf.bits_per_sample = bps
        cpu.regs[EAX] = DS_OK

    def _buf_stop(cpu: "CPU", mem: "Memory") -> None:
        buf = _buf_this(cpu, mem)
        if buf:
            with _ds_buf_lock:
                buf.playing = False
        cpu.regs[EAX] = DS_OK

    buf_vtable = [
        # [0]  QueryInterface(REFIID, void**)
        _com_stub(stubs, "dsound.dll", "Buf::QueryInterface",
                  _buf_query_interface, 8, memory),
        # [1]  AddRef()
        _com_stub(stubs, "dsound.dll", "Buf::AddRef",
                  lambda cpu, mem: _set_eax(cpu, 2), 0, memory),
        # [2]  Release()
        _com_stub(stubs, "dsound.dll", "Buf::Release",
                  lambda cpu, mem: _set_eax(cpu, 1), 0, memory),
        # [3]  GetCaps(lpDSBCaps)
        _com_stub(stubs, "dsound.dll", "Buf::GetCaps",
                  _buf_get_caps, 4, memory),
        # [4]  GetCurrentPosition(lpdwPlayCursor, lpdwWriteCursor)
        _com_stub(stubs, "dsound.dll", "Buf::GetCurrentPosition",
                  _buf_get_current_position, 8, memory),
        # [5]  GetFormat(lpwfxFormat, dwSizeAllocated, lpdwSizeWritten)
        _com_stub(stubs, "dsound.dll", "Buf::GetFormat",
                  _buf_get_format, 12, memory),
        # [6]  GetVolume(lplVolume)
        _com_stub(stubs, "dsound.dll", "Buf::GetVolume",
                  lambda cpu, mem: _set_eax(cpu, DS_OK), 4, memory),
        # [7]  GetPan(lplPan)
        _com_stub(stubs, "dsound.dll", "Buf::GetPan",
                  lambda cpu, mem: _set_eax(cpu, DS_OK), 4, memory),
        # [8]  GetFrequency(lpdwFrequency)
        _com_stub(stubs, "dsound.dll", "Buf::GetFrequency",
                  lambda cpu, mem: _set_eax(cpu, DS_OK), 4, memory),
        # [9]  GetStatus(lpdwStatus)
        _com_stub(stubs, "dsound.dll", "Buf::GetStatus",
                  _buf_get_status, 4, memory),
        # [10] Initialize(lpDirectSound, lpcwfxFormat)
        _com_stub(stubs, "dsound.dll", "Buf::Initialize",
                  lambda cpu, mem: _set_eax(cpu, DSERR_ALREADYINITIALIZED), 8, memory),
        # [11] Lock(dwWriteCursor, dwWriteBytes, lplpvAudioPtr1, lpdwAudioBytes1,
        #           lplpvAudioPtr2, lpdwAudioBytes2, dwFlags)
        _com_stub(stubs, "dsound.dll", "Buf::Lock",
                  _buf_lock, 28, memory),
        # [12] Play(dwReserved1, dwPriority, dwFlags)
        _com_stub(stubs, "dsound.dll", "Buf::Play",
                  _buf_play, 12, memory),
        # [13] SetCurrentPosition(dwNewPosition)
        _com_stub(stubs, "dsound.dll", "Buf::SetCurrentPosition",
                  _buf_set_current_position, 4, memory),
        # [14] SetFormat(lpcfxFormat)
        _com_stub(stubs, "dsound.dll", "Buf::SetFormat",
                  _buf_set_format, 4, memory),
        # [15] SetVolume(lVolume)
        _com_stub(stubs, "dsound.dll", "Buf::SetVolume",
                  lambda cpu, mem: _set_eax(cpu, DS_OK), 4, memory),
        # [16] SetPan(lPan)
        _com_stub(stubs, "dsound.dll", "Buf::SetPan",
                  lambda cpu, mem: _set_eax(cpu, DS_OK), 4, memory),
        # [17] SetFrequency(dwFrequency)
        _com_stub(stubs, "dsound.dll", "Buf::SetFrequency",
                  lambda cpu, mem: _set_eax(cpu, DS_OK), 4, memory),
        # [18] Stop()
        _com_stub(stubs, "dsound.dll", "Buf::Stop",
                  _buf_stop, 0, memory),
        # [19] Unlock(lpvAudioPtr1, dwAudioBytes1, lpvAudioPtr2, dwAudioBytes2)
        _com_stub(stubs, "dsound.dll", "Buf::Unlock",
                  lambda cpu, mem: _set_eax(cpu, DS_OK), 16, memory),
        # [20] Restore()
        _com_stub(stubs, "dsound.dll", "Buf::Restore",
                  lambda cpu, mem: _set_eax(cpu, DS_OK), 0, memory),
    ]
    for i, addr in enumerate(buf_vtable):
        memory.write32(DS_BUF_VTABLE + i * 4, addr)

    # ── DirectSoundCreate DLL export (ordinal 1 + by name) ────────────────

    def _direct_sound_create(cpu: "CPU") -> None:
        # DirectSoundCreate(lpGUID, ppDS, pUnkOuter) — 12 bytes, stdcall
        from tew.api.win32_handlers import cleanup_stdcall
        pp_ds = memory.read32((cpu.regs[ESP] + 8) & 0xFFFFFFFF)
        if pp_ds:
            memory.write32(pp_ds, DS_OBJ)
        logger.info("handlers", f"DirectSoundCreate -> DS_OBJ=0x{DS_OBJ:08x}")
        cpu.regs[EAX] = DS_OK
        cleanup_stdcall(cpu, memory, 12)

    stubs.register_handler("dsound.dll", "DirectSoundCreate",  _direct_sound_create)
    stubs.register_handler("dsound.dll", "Ordinal #1",          _direct_sound_create)
    stubs.register_handler("dsound.dll", "DirectSoundCreate8", _direct_sound_create)

    logger.info("handlers",
        "DirectSound handlers registered — IDirectSound + IDirectSoundBuffer with SDL2 audio")
