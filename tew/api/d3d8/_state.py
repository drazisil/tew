"""D3D8 Vulkan runtime state — populated by Direct3DCreate8 and CreateDevice."""

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
