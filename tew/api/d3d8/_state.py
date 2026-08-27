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

# Swapchain + image resources.
_vk_swapchain = None
_vk_swapchain_format: int = 0        # VkFormat integer
_vk_swapchain_images: list = []      # list of VkImage handles
_vk_swapchain_width: int = 0
_vk_swapchain_height: int = 0

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

# True while inside a vkCmdBeginRenderPass / vkCmdEndRenderPass pair.
_vk_in_render_pass: bool = False

# Stream source set by SetStreamSource — used by DrawPrimitive.
_draw_stream_ptr:    int = 0   # flat-memory address of bound vertex buffer data
_draw_stream_stride: int = 0   # stride in bytes

# Vertex FVF/handle set by SetVertexShader.
_draw_vertex_fvf: int = 0

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
