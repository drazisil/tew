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

# Device-level extension functions loaded after vkCreateDevice.
_vk_fn_create_swapchain = None
_vk_fn_get_swapchain_images = None
_vk_fn_acquire_next_image = None
_vk_fn_queue_present = None
