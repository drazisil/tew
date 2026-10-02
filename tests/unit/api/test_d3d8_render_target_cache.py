"""Regression test for Dev::GetRenderTarget/GetDepthStencilSurface
fabricating a brand-new surface object on every call (2026-09-05 fix).

Real D3D8 AddRef's and returns the SAME underlying surface every call --
the caller's matching Release() only drops their own reference, since the
device keeps its own internal one. The old handlers instead allocated a
fresh, independently-ref-counted surface object on every call, so the
game's single, correct Release() immediately freed tew's only copy of it.
The freed heap address was then handed to an unrelated later allocation,
whose write into the object header field (D3DFORMAT, at offset 20)
corrupted the format seen by the *original*, still-in-use surface --
confirmed live: a 1536x1248 background surface's UnlockRect calls flipped
from a real color format to a depth-stencil format partway through, and a
Surface::UnlockRect call succeeded on the address 23+ seconds after tew's
own bookkeeping had already freed it.

Fixed by caching one canonical backbuffer / depth-stencil surface object
per device and AddRef'ing (not reallocating) on repeat calls.
"""
from __future__ import annotations

import pytest

import tew.api.d3d8._state as state
from tew.api.d3d8._helpers import _alloc_registry
from tew.api.d3d8._layout import D3DDEV_OBJ, S_OK
from tew.api.d3d8.idirect3d8device import make_vtable
from tew.api.d3d8.idirect3d8resource import _ref_counts, _release
from tew.hardware.cpu_zig import EAX, ESP
from tew.hardware.memory import Memory


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
STACK    = 0x100000


@pytest.fixture(autouse=True)
def _reset_state():
    state._vk_backbuffer_surface_obj = None
    state._vk_depth_stencil_surface_obj = None
    state._vk_swapchain_width = 1536
    state._vk_swapchain_height = 1248
    _ref_counts.clear()
    _alloc_registry.clear()
    yield
    state._vk_backbuffer_surface_obj = None
    state._vk_depth_stencil_surface_obj = None
    _ref_counts.clear()
    _alloc_registry.clear()


@pytest.fixture
def env():
    mem = Memory(MEM_SIZE)
    cpu = _FakeCPU()
    stubs = _StubHandlers()
    make_vtable(stubs, mem, window_manager=None)
    return cpu, mem, stubs


def get_render_target(cpu, mem, stubs) -> int:
    pp_surf = STACK + 100
    cpu.regs[ESP] = STACK
    mem.write32(STACK + 4, D3DDEV_OBJ)
    mem.write32(STACK + 8, pp_surf)
    stubs.get("d3d8dev", "Dev::GetRenderTarget")(cpu)
    return mem.read32(pp_surf)


def get_depth_stencil(cpu, mem, stubs) -> int:
    pp_surf = STACK + 100
    cpu.regs[ESP] = STACK
    mem.write32(STACK + 4, D3DDEV_OBJ)
    mem.write32(STACK + 8, pp_surf)
    stubs.get("d3d8dev", "Dev::GetDepthStencilSurface")(cpu)
    return mem.read32(pp_surf)


class TestRenderTargetIsCached:

    def test_repeat_calls_return_same_object(self, env):
        cpu, mem, stubs = env
        first = get_render_target(cpu, mem, stubs)
        second = get_render_target(cpu, mem, stubs)
        assert first == second

    def test_repeat_calls_addref_instead_of_reallocating(self, env):
        cpu, mem, stubs = env
        obj = get_render_target(cpu, mem, stubs)
        get_render_target(cpu, mem, stubs)
        get_render_target(cpu, mem, stubs)
        assert _ref_counts[obj] == 3

    def test_single_release_does_not_free_a_still_referenced_target(self, env):
        # This is the exact bug: the caller's single, correct Release()
        # after one GetRenderTarget() call must NOT free the object, since
        # a second logical caller (e.g. the device's own internal render
        # loop) is assumed to hold a reference too in real D3D8 usage --
        # modeled here as two independent GetRenderTarget() calls.
        cpu, mem, stubs = env
        obj = get_render_target(cpu, mem, stubs)
        get_render_target(cpu, mem, stubs)  # second logical reference
        cpu.regs[ESP] = STACK
        mem.write32(STACK + 4, obj)
        _release(cpu, mem)
        assert obj in _alloc_registry, "object was freed while still referenced"

    def test_returns_s_ok(self, env):
        cpu, mem, stubs = env
        get_render_target(cpu, mem, stubs)
        assert cpu.regs[EAX] == S_OK


class TestDepthStencilIsCached:

    def test_repeat_calls_return_same_object(self, env):
        cpu, mem, stubs = env
        first = get_depth_stencil(cpu, mem, stubs)
        second = get_depth_stencil(cpu, mem, stubs)
        assert first == second

    def test_repeat_calls_addref_instead_of_reallocating(self, env):
        cpu, mem, stubs = env
        obj = get_depth_stencil(cpu, mem, stubs)
        get_depth_stencil(cpu, mem, stubs)
        assert _ref_counts[obj] == 2

    def test_distinct_from_render_target_object(self, env):
        cpu, mem, stubs = env
        rt = get_render_target(cpu, mem, stubs)
        ds = get_depth_stencil(cpu, mem, stubs)
        assert rt != ds


def get_back_buffer(cpu, mem, stubs) -> int:
    pp_surf = STACK + 100
    cpu.regs[ESP] = STACK
    mem.write32(STACK + 4, D3DDEV_OBJ)
    mem.write32(STACK + 8, 0)   # BackBuffer index
    mem.write32(STACK + 12, 0)  # D3DBACKBUFFER_TYPE_MONO
    mem.write32(STACK + 16, pp_surf)
    stubs.get("d3d8dev", "Dev::GetBackBuffer")(cpu)
    return mem.read32(pp_surf)


class TestBackBufferIsCached:
    """GetBackBuffer used to allocate a fresh width*height*4 surface per call;
    dx8z.dll's repeated calls exhausted the D3D8 heap (51 x 1,920,000 bytes)."""

    def test_repeat_calls_return_same_object(self, env):
        cpu, mem, stubs = env
        assert get_back_buffer(cpu, mem, stubs) == get_back_buffer(cpu, mem, stubs)

    def test_repeat_calls_do_not_grow_the_registry(self, env):
        cpu, mem, stubs = env
        get_back_buffer(cpu, mem, stubs)
        before = len(_alloc_registry)
        for _ in range(60):
            get_back_buffer(cpu, mem, stubs)
        assert len(_alloc_registry) == before

    def test_caller_release_does_not_free_the_device_copy(self, env):
        cpu, mem, stubs = env
        obj = get_back_buffer(cpu, mem, stubs)
        cpu.regs[ESP] = STACK
        mem.write32(STACK + 4, obj)
        _release(cpu, mem)
        assert obj in _alloc_registry
        assert get_back_buffer(cpu, mem, stubs) == obj

    def test_shared_with_get_render_target(self, env):
        cpu, mem, stubs = env
        assert get_back_buffer(cpu, mem, stubs) == get_render_target(cpu, mem, stubs)

    def test_recreated_if_object_was_freed(self, env):
        cpu, mem, stubs = env
        obj = get_back_buffer(cpu, mem, stubs)
        for _ in range(2):
            cpu.regs[ESP] = STACK
            mem.write32(STACK + 4, obj)
            _release(cpu, mem)
        assert obj not in _alloc_registry
        again = get_back_buffer(cpu, mem, stubs)
        assert again in _alloc_registry
