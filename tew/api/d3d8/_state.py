"""D3D8 Vulkan runtime state — populated by Direct3DCreate8 and CreateDevice."""

from tew.logger import logger

# VkInstance created during Direct3DCreate8.
_vk_instance = None

# List of VkPhysicalDevice handles enumerated at instance creation time.
_vk_physical_devices: list = []

# Logical device + queue family indices + queue handles (set during CreateDevice).
_vk_device = None
_vk_graphics_queue_family: int = -1
_vk_present_queue_family: int = -1
_vk_graphics_queue = None
_vk_present_queue = None

# VkSurfaceKHR created from the SDL window in CreateDevice.
_vk_surface = None

# HWND the SDL window was resolved from in CreateDevice. Reset's
# D3DPRESENT_PARAMETERS.hDeviceWindow is commonly 0 (meaning "reuse the
# window CreateDevice was given"), so Reset looks this up instead of
# re-parsing hDeviceWindow.
_vk_hwnd: int = 0

# Swapchain + image resources.
_vk_swapchain = None
_vk_swapchain_format: int = 0        # VkFormat integer
_vk_swapchain_images: list = []      # list of VkImage handles
_vk_swapchain_width: int = 0
_vk_swapchain_height: int = 0

# The game's own requested D3DPRESENT_PARAMETERS BackBufferWidth/Height, as
# opposed to _vk_swapchain_width/height (the real physical window/swapchain
# size, which WINDOW_SCALE in idirect3d8.py may enlarge for display).
# DrawPrimitive and every guest-observable surface size (GetBackBuffer,
# GetRenderTarget, GetDepthStencilSurface) must use this logical size, not
# the physical one, or vertex screen-space coordinates the game computed
# assuming its requested resolution get normalized against the wrong extent.
_vk_logical_width: int = 0
_vk_logical_height: int = 0

# Command pool / single reusable command buffer.
_vk_command_pool = None
_vk_cmd_buf = None

# Frame sync primitives.
_vk_image_available = None   # VkSemaphore: signalled by vkAcquireNextImageKHR
_vk_render_done = None       # VkSemaphore: signalled after vkQueueSubmit
_vk_in_flight = None         # VkFence: CPU/GPU frame boundary

# Index into _vk_swapchain_images for the current frame (set by BeginScene).
_vk_current_image_idx: int = 0

# True after QueueSubmit in Present; cleared by BeginScene.
# Used to skip vkWaitForFences when no frame was submitted (BeginScene called
# without an intervening Present, which D3D8 allows).
_vk_frame_submitted: bool = False

# True after AcquireNextImage in BeginScene; cleared by Present.
# Used to skip re-acquiring when BeginScene is called again without Present.
_vk_image_acquired: bool = False

# Pipeline resources (created in CreateDevice after swapchain is ready).
_vk_image_views:      list = []   # VkImageView per swapchain image
_vk_render_pass:      object = None
_vk_framebuffers:     list = []   # VkFramebuffer per swapchain image
_vk_pipeline:         object = None
_vk_pipeline_layout:  object = None
_vk_vertex_buffer:    object = None
_vk_vertex_memory:    object = None
_vk_vertex_mapped_ptr: object = None  # ctypes void* from vkMapMemory (persistent)
_vk_vertex_buffer_size: int = 0

# Byte offset into _vk_vertex_buffer for the NEXT DrawPrimitive's vertex data.
# Every draw in a frame gets its own region instead of all sharing offset 0:
# vkCmdBindVertexBuffers/vkCmdDraw don't snapshot buffer contents at record
# time, only at actual GPU execution (Present's vkQueueSubmit) -- since many
# BeginScene/DrawPrimitive calls can accumulate into one command buffer
# before a Present ever happens, writing every draw's vertices to the same
# offset 0 meant every draw in that frame read back whichever draw wrote
# last, not its own data (confirmed live via direct GPU pixel readback: a
# correctly-recorded, real-textured, non-degenerate draw came back as pure
# black because a later degenerate draw in the same frame overwrote its
# vertex data before the GPU ever read it). Reset to 0 at the start of each
# new frame (BeginScene's fresh image-acquire path, not a same-frame
# continuation).
_vk_vertex_cursor: int = 0

# Swapchain image indices that have completed at least one full
# BeginScene->Present cycle. BeginScene's per-frame re-acquire barrier used
# oldLayout=UNDEFINED unconditionally, which is a real content-discard hint
# in Vulkan (some drivers honor it literally) -- correct only the very first
# time each image is used, since D3D8's Clear() is meant to be the only
# thing that erases prior backbuffer content, not every frame's re-acquire.
# Reset whenever the swapchain is (re)created, since a fresh swapchain's
# images are genuinely undefined again.
_vk_swapchain_images_used: set = set()

# True while inside a vkCmdBeginRenderPass / vkCmdEndRenderPass pair.
_vk_in_render_pass: bool = False

# Stream source set by SetStreamSource — used by DrawPrimitive.
_draw_stream_ptr:    int = 0   # flat-memory address of bound vertex buffer data
_draw_stream_stride: int = 0   # stride in bytes

# Vertex FVF/handle set by SetVertexShader.
_draw_vertex_fvf: int = 0

# Descriptor set / sampler / default white texture (created once in
# CreateDevice, alongside the rest of the pipeline).
_vk_descriptor_set_layout: object = None
_vk_descriptor_pool:       object = None
_vk_descriptor_set:        object = None
_vk_sampler:               object = None
_vk_default_tex_image:     object = None
_vk_default_tex_memory:    object = None
_vk_default_tex_view:      object = None

# Bound IDirect3DBaseTexture8* per sampler stage, set by SetTexture (0 = none).
_bound_textures: dict[int, int] = {}

# (stage, D3DTEXTURESTAGESTATETYPE) -> DWORD value, set by SetTextureStageState.
_texture_stage_state: dict[tuple[int, int], int] = {}

# Cached canonical IDirect3DSurface8* for the primary render target / depth-
# stencil surface, lazily created on first GetRenderTarget()/
# GetDepthStencilSurface() call. Real D3D8 AddRef's and returns the SAME
# underlying surface every call -- the caller's matching Release() only
# drops their reference, since the device keeps its own. Fabricating a
# fresh, independently-ref-counted object on every call (the old behaviour)
# meant the game's single, correct Release() immediately freed tew's only
# copy of it -- confirmed live: a 1536x1248 surface's format field got
# clobbered by an unrelated later allocation reusing the same freed heap
# address, while the game kept calling UnlockRect on it 20+ seconds later.
_vk_backbuffer_surface_obj:    int | None = None
_vk_depth_stencil_surface_obj: int | None = None

# Real SDL cursor set via IDirect3DDevice8::SetCursorProperties (previously a
# lying no-op stub that returned S_OK without ever telling SDL to display a
# cursor). None until the game sets one; freed and replaced on each new
# SetCursorProperties call to avoid leaking prior cursors (e.g. animation).
_cursor_sdl_handle: object = None
_cursor_shown: bool = False

# Instance-level extension functions loaded after vkCreateInstance.
_vk_fn_get_surface_caps = None   # vkGetPhysicalDeviceSurfaceCapabilitiesKHR

# Device-level extension functions loaded after vkCreateDevice.
_vk_fn_create_swapchain = None
_vk_fn_destroy_swapchain = None
_vk_fn_get_swapchain_images = None
_vk_fn_acquire_next_image = None
_vk_fn_queue_present = None


def shutdown() -> None:
    """Tears down every Vulkan object this module created, in reverse
    creation order, before the SDL window they're bound to gets destroyed.

    Never wired in before 2026-08-24: `WindowManager.shutdown()` (tew/api/
    window_manager.py) called `SDL_DestroyWindow` directly on process exit
    (including now on SIGTERM, e.g. from a `timeout`-bounded debugging run)
    with no Vulkan-side cleanup at all -- destroying the surface's native
    window handle out from under a still-live VkSwapchainKHR/VkSurfaceKHR/
    VkDevice/VkInstance. This is undefined behavior per the Vulkan spec and
    was suspected of destabilizing the desktop compositor (KWin) across a
    night of `timeout`-killed runs. `vkDeviceWaitIdle` first ensures the GPU
    isn't still using anything about to be destroyed; everything else uses
    a bare `except Exception: pass` since this runs during shutdown/signal
    handling -- a failed destroy call here must never block process exit,
    and there's nothing meaningful to recover into if one does fail.
    """
    import vulkan as vk

    if _vk_device is not None:
        try:
            vk.vkDeviceWaitIdle(_vk_device)
        except Exception:
            pass

    if _vk_swapchain is not None and _vk_fn_destroy_swapchain is not None:
        try:
            _vk_fn_destroy_swapchain(_vk_device, _vk_swapchain, None)
        except Exception:
            pass

    if _vk_image_available is not None:
        try:
            vk.vkDestroySemaphore(_vk_device, _vk_image_available, None)
        except Exception:
            pass
    if _vk_render_done is not None:
        try:
            vk.vkDestroySemaphore(_vk_device, _vk_render_done, None)
        except Exception:
            pass
    if _vk_in_flight is not None:
        try:
            vk.vkDestroyFence(_vk_device, _vk_in_flight, None)
        except Exception:
            pass
    if _vk_command_pool is not None:
        try:
            vk.vkDestroyCommandPool(_vk_device, _vk_command_pool, None)
        except Exception:
            pass

    if _vk_surface is not None and _vk_instance is not None:
        try:
            vk.vkDestroySurfaceKHR(_vk_instance, _vk_surface, None)
        except Exception:
            pass

    if _vk_device is not None:
        try:
            vk.vkDestroyDevice(_vk_device, None)
        except Exception:
            pass

    if _vk_instance is not None:
        try:
            vk.vkDestroyInstance(_vk_instance, None)
        except Exception:
            pass

    logger.info("d3d8", "[shutdown] Vulkan objects torn down")
