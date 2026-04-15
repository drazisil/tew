"""D3D8 Vulkan runtime state — populated by Direct3DCreate8."""

# VkInstance created during Direct3DCreate8.
_vk_instance = None

# List of VkPhysicalDevice handles enumerated at instance creation time.
_vk_physical_devices: list = []
