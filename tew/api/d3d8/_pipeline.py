"""Vulkan render pass, framebuffer, pipeline, and vertex buffer setup for DrawPrimitive.

Shaders are encoded as SPIR-V binary (no external compiler required; validated
with `spirv-val` and by building a real headless VkPipeline before being
wired into the emulator -- see /tmp/vk_shader_test.py from the session that
added texture sampling).

Vertex format uploaded to Vulkan: 40 bytes per vertex:
  [0]  X_ndc  f32   = (screen_x / vp_w) * 2 - 1
  [4]  Y_ndc  f32   = 1 - (screen_y / vp_h) * 2
  [8]  Z      f32
  [12] W      f32   = 1.0
  [16] B      f32   = (diffuse >>  0 & 0xFF) / 255
  [20] G      f32   = (diffuse >>  8 & 0xFF) / 255
  [24] R      f32   = (diffuse >> 16 & 0xFF) / 255
  [28] A      f32   = (diffuse >> 24 & 0xFF) / 255
  [32] U      f32   texture coordinate
  [36] V      f32   texture coordinate

The fragment shader samples a single combined-image-sampler (set=0, binding=0)
and multiplies it by the interpolated diffuse color -- standard D3D8 stage-0
MODULATE. Untextured draws (SetTexture(stage, NULL)) point the descriptor at
a 1x1 opaque-white texture instead of branching in the shader, so sampling
always has a valid binding and untextured geometry still renders as pure
diffuse color (white * color == color).
"""

from __future__ import annotations

import struct
import ctypes
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from tew.logger import logger

# ── SPIR-V encoding helpers ───────────────────────────────────────────────────

def _encode_spv(words: list[int]) -> bytes:
    """Pack a list of 32-bit words into SPIR-V bytes (little-endian)."""
    return struct.pack(f"<{len(words)}I", *words)


# ── Vertex shader: pos/color/uv passthrough ───────────────────────────────────
# Inputs:  location 0 = vec4 pos (NDC xyzw), location 1 = vec4 color (BGRA floats),
#          location 2 = vec2 uv
# Outputs: gl_Position = pos, location 0 = color, location 1 = uv
#
# ID assignments:
#  1=void  2=float  3=vec4  4=fn_type  5=main  6=ptr_in_v4  7=ptr_out_v4
#  8=gl_PerVertex  9=ptr_out_pv  10=in_pos  11=in_color  12=out_color
#  13=gl_pervert  14=int32  15=c0  16=entry  17=pos  18=col  19=gl_pos_chain
#  20=vec2  21=ptr_in_v2  22=ptr_out_v2  23=in_uv  24=out_uv  25=uv
#  bound=26
_VERT_WORDS: list[int] = [
    # SPIR-V header
    0x07230203, 0x00010300, 0x00000000, 26, 0x00000000,
    # OpCapability Shader
    0x00020011, 1,
    # OpMemoryModel Logical GLSL450
    0x0003000E, 0, 1,
    # OpEntryPoint Vertex %main "main"  %in_pos %in_color %in_uv %out_color %out_uv %gl_pervert
    0x000B000F, 0, 5, 0x6e69616d, 0x00000000, 10, 11, 23, 12, 24, 13,
    # OpDecorate %in_pos    Location 0
    0x00040047, 10, 30, 0,
    # OpDecorate %in_color  Location 1
    0x00040047, 11, 30, 1,
    # OpDecorate %in_uv     Location 2
    0x00040047, 23, 30, 2,
    # OpDecorate %out_color Location 0
    0x00040047, 12, 30, 0,
    # OpDecorate %out_uv    Location 1
    0x00040047, 24, 30, 1,
    # OpMemberDecorate %gl_PerVertex 0 BuiltIn Position
    0x00050048, 8, 0, 11, 0,
    # OpDecorate %gl_PerVertex Block
    0x00030047, 8, 2,
    # %void(1) = OpTypeVoid
    0x00020013, 1,
    # %float(2) = OpTypeFloat 32
    0x00030016, 2, 32,
    # %vec4(3) = OpTypeVector %float(2) 4
    0x00040017, 3, 2, 4,
    # %fn_type(4) = OpTypeFunction %void(1)
    0x00030021, 4, 1,
    # %ptr_in_v4(6) = OpTypePointer Input %vec4(3)
    0x00040020, 6, 1, 3,
    # %ptr_out_v4(7) = OpTypePointer Output %vec4(3)
    0x00040020, 7, 3, 3,
    # %gl_PerVertex(8) = OpTypeStruct { %vec4(3) }
    0x0003001E, 8, 3,
    # %ptr_out_pv(9) = OpTypePointer Output %gl_PerVertex(8)
    0x00040020, 9, 3, 8,
    # %int32(14) = OpTypeInt 32 1
    0x00040015, 14, 32, 1,
    # %c0(15) = OpConstant %int32(14) 0
    0x0004002B, 14, 15, 0,
    # %vec2(20) = OpTypeVector %float(2) 2
    0x00040017, 20, 2, 2,
    # %ptr_in_v2(21) = OpTypePointer Input %vec2(20)
    0x00040020, 21, 1, 20,
    # %ptr_out_v2(22) = OpTypePointer Output %vec2(20)
    0x00040020, 22, 3, 20,
    # %in_pos(10)    = OpVariable %ptr_in_v4(6) Input
    0x0004003B, 6, 10, 1,
    # %in_color(11)  = OpVariable %ptr_in_v4(6) Input
    0x0004003B, 6, 11, 1,
    # %in_uv(23)     = OpVariable %ptr_in_v2(21) Input
    0x0004003B, 21, 23, 1,
    # %out_color(12) = OpVariable %ptr_out_v4(7) Output
    0x0004003B, 7, 12, 3,
    # %out_uv(24)    = OpVariable %ptr_out_v2(22) Output
    0x0004003B, 22, 24, 3,
    # %gl_pervert(13) = OpVariable %ptr_out_pv(9) Output
    0x0004003B, 9, 13, 3,
    # %main(5) = OpFunction %void(1) None %fn_type(4)
    0x00050036, 1, 5, 0, 4,
    # %entry(16) = OpLabel
    0x000200F8, 16,
    # %pos(17) = OpLoad %vec4(3) %in_pos(10)
    0x0004003D, 3, 17, 10,
    # %col(18) = OpLoad %vec4(3) %in_color(11)
    0x0004003D, 3, 18, 11,
    # %uv(25) = OpLoad %vec2(20) %in_uv(23)
    0x0004003D, 20, 25, 23,
    # %gl_pos_chain(19) = OpAccessChain %ptr_out_v4(7) %gl_pervert(13) %c0(15)
    0x00050041, 7, 19, 13, 15,
    # OpStore %gl_pos_chain(19) %pos(17)
    0x0003003E, 19, 17,
    # OpStore %out_color(12) %col(18)
    0x0003003E, 12, 18,
    # OpStore %out_uv(24) %uv(25)
    0x0003003E, 24, 25,
    # OpReturn
    0x000100FD,
    # OpFunctionEnd
    0x00010038,
]
VERT_SPV: bytes = _encode_spv(_VERT_WORDS)


# ── Fragment shader: sample stage-0 texture, modulate by diffuse ─────────────
# Inputs:  location 0 = vec4 color, location 1 = vec2 uv
# Uniform: set=0 binding=0 = sampler2D
# Output:  location 0 = texture(sampler, uv) * color
#
# ID assignments:
#  1=void  2=float  3=vec4  4=fn_type  5=main  6=ptr_in_v4  7=ptr_out_v4
#  8=in_color  9=out_color  10=entry  11=col  12=vec2  13=ptr_in_v2  14=in_uv
#  15=uv  16=image_type  17=sampled_image_type  18=ptr_uniformconstant_sampled
#  19=tex_var  20=loaded_sampled_image  21=sampled_color  22=final_color
#  bound=23
_FRAG_WORDS: list[int] = [
    # SPIR-V header
    0x07230203, 0x00010300, 0x00000000, 23, 0x00000000,
    # OpCapability Shader
    0x00020011, 1,
    # OpMemoryModel Logical GLSL450
    0x0003000E, 0, 1,
    # OpEntryPoint Fragment(4) %main(5) "main"  %in_color(8) %in_uv(14) %out_color(9)
    0x0008000F, 4, 5, 0x6e69616d, 0x00000000, 8, 14, 9,
    # OpExecutionMode %main(5) OriginUpperLeft(7)
    0x00030010, 5, 7,
    # OpDecorate %in_color(8)  Location 0
    0x00040047, 8, 30, 0,
    # OpDecorate %in_uv(14)    Location 1
    0x00040047, 14, 30, 1,
    # OpDecorate %out_color(9) Location 0
    0x00040047, 9, 30, 0,
    # OpDecorate %tex_var(19)  Binding 0
    0x00040047, 19, 33, 0,
    # OpDecorate %tex_var(19)  DescriptorSet 0
    0x00040047, 19, 34, 0,
    # %void(1) = OpTypeVoid
    0x00020013, 1,
    # %float(2) = OpTypeFloat 32
    0x00030016, 2, 32,
    # %vec4(3) = OpTypeVector %float(2) 4
    0x00040017, 3, 2, 4,
    # %fn_type(4) = OpTypeFunction %void(1)
    0x00030021, 4, 1,
    # %ptr_in_v4(6) = OpTypePointer Input %vec4(3)
    0x00040020, 6, 1, 3,
    # %ptr_out_v4(7) = OpTypePointer Output %vec4(3)
    0x00040020, 7, 3, 3,
    # %vec2(12) = OpTypeVector %float(2) 2
    0x00040017, 12, 2, 2,
    # %ptr_in_v2(13) = OpTypePointer Input %vec2(12)
    0x00040020, 13, 1, 12,
    # %image_type(16) = OpTypeImage %float(2) 2D 0 0 0 1 Unknown
    0x00090019, 16, 2, 1, 0, 0, 0, 1, 0,
    # %sampled_image_type(17) = OpTypeSampledImage %image_type(16)
    0x0003001B, 17, 16,
    # %ptr_uniformconstant_sampled(18) = OpTypePointer UniformConstant %sampled_image_type(17)
    0x00040020, 18, 0, 17,
    # %in_color(8)  = OpVariable %ptr_in_v4(6) Input
    0x0004003B, 6, 8, 1,
    # %out_color(9) = OpVariable %ptr_out_v4(7) Output
    0x0004003B, 7, 9, 3,
    # %in_uv(14)    = OpVariable %ptr_in_v2(13) Input
    0x0004003B, 13, 14, 1,
    # %tex_var(19)  = OpVariable %ptr_uniformconstant_sampled(18) UniformConstant
    0x0004003B, 18, 19, 0,
    # %main(5) = OpFunction %void(1) None %fn_type(4)
    0x00050036, 1, 5, 0, 4,
    # %entry(10) = OpLabel
    0x000200F8, 10,
    # %col(11) = OpLoad %vec4(3) %in_color(8)
    0x0004003D, 3, 11, 8,
    # %uv(15) = OpLoad %vec2(12) %in_uv(14)
    0x0004003D, 12, 15, 14,
    # %loaded_sampled_image(20) = OpLoad %sampled_image_type(17) %tex_var(19)
    0x0004003D, 17, 20, 19,
    # %sampled_color(21) = OpImageSampleImplicitLod %vec4(3) %loaded_sampled_image(20) %uv(15)
    0x00050057, 3, 21, 20, 15,
    # %final_color(22) = OpFMul %vec4(3) %sampled_color(21) %col(11)
    0x00050085, 3, 22, 21, 11,
    # OpStore %out_color(9) %final_color(22)
    0x0003003E, 9, 22,
    # OpReturn
    0x000100FD,
    # OpFunctionEnd
    0x00010038,
]
FRAG_SPV: bytes = _encode_spv(_FRAG_WORDS)


# ── Pipeline creation functions ───────────────────────────────────────────────

def create_image_views(device, images: list, fmt: int) -> list:
    """Create a VkImageView for each swapchain image."""
    import vulkan as vk
    views = []
    for img in images:
        ci = vk.VkImageViewCreateInfo(
            sType=vk.VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,
            image=img,
            viewType=vk.VK_IMAGE_VIEW_TYPE_2D,
            format=fmt,
            components=vk.VkComponentMapping(
                r=vk.VK_COMPONENT_SWIZZLE_IDENTITY,
                g=vk.VK_COMPONENT_SWIZZLE_IDENTITY,
                b=vk.VK_COMPONENT_SWIZZLE_IDENTITY,
                a=vk.VK_COMPONENT_SWIZZLE_IDENTITY,
            ),
            subresourceRange=vk.VkImageSubresourceRange(
                aspectMask=vk.VK_IMAGE_ASPECT_COLOR_BIT,
                baseMipLevel=0, levelCount=1,
                baseArrayLayer=0, layerCount=1,
            ),
        )
        views.append(vk.vkCreateImageView(device, ci, None))
    return views


def create_render_pass(device, color_format: int):
    """Create a render pass with one color attachment that loads existing content.

    loadOp=LOAD preserves the cleared background written by vkCmdClearColorImage.
    initialLayout must match the layout the image is in when the render pass begins;
    we transition to COLOR_ATTACHMENT_OPTIMAL before BeginRenderPass.
    """
    import vulkan as vk
    attachment = vk.VkAttachmentDescription(
        format=color_format,
        samples=vk.VK_SAMPLE_COUNT_1_BIT,
        loadOp=vk.VK_ATTACHMENT_LOAD_OP_LOAD,
        storeOp=vk.VK_ATTACHMENT_STORE_OP_STORE,
        stencilLoadOp=vk.VK_ATTACHMENT_LOAD_OP_DONT_CARE,
        stencilStoreOp=vk.VK_ATTACHMENT_STORE_OP_DONT_CARE,
        initialLayout=vk.VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
        finalLayout=vk.VK_IMAGE_LAYOUT_PRESENT_SRC_KHR,
    )
    color_ref = vk.VkAttachmentReference(
        attachment=0,
        layout=vk.VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
    )
    subpass = vk.VkSubpassDescription(
        pipelineBindPoint=vk.VK_PIPELINE_BIND_POINT_GRAPHICS,
        colorAttachmentCount=1,
        pColorAttachments=[color_ref],
    )
    dependency = vk.VkSubpassDependency(
        srcSubpass=vk.VK_SUBPASS_EXTERNAL,
        dstSubpass=0,
        srcStageMask=vk.VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
        dstStageMask=vk.VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
        srcAccessMask=0,
        dstAccessMask=vk.VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,
    )
    rp_ci = vk.VkRenderPassCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO,
        attachmentCount=1,
        pAttachments=[attachment],
        subpassCount=1,
        pSubpasses=[subpass],
        dependencyCount=1,
        pDependencies=[dependency],
    )
    return vk.vkCreateRenderPass(device, rp_ci, None)


def create_framebuffers(device, render_pass, image_views: list,
                        width: int, height: int) -> list:
    """Create a VkFramebuffer for each swapchain image view."""
    import vulkan as vk
    fbs = []
    for view in image_views:
        ci = vk.VkFramebufferCreateInfo(
            sType=vk.VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO,
            renderPass=render_pass,
            attachmentCount=1,
            pAttachments=[view],
            width=width,
            height=height,
            layers=1,
        )
        fbs.append(vk.vkCreateFramebuffer(device, ci, None))
    return fbs


def create_descriptor_set_layout(device):
    """Descriptor set layout for one stage-0 combined image sampler."""
    import vulkan as vk
    binding = vk.VkDescriptorSetLayoutBinding(
        binding=0,
        descriptorType=vk.VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
        descriptorCount=1,
        stageFlags=vk.VK_SHADER_STAGE_FRAGMENT_BIT,
    )
    ci = vk.VkDescriptorSetLayoutCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
        bindingCount=1, pBindings=[binding],
    )
    return vk.vkCreateDescriptorSetLayout(device, ci, None)


# Descriptor sets are mutable GPU-visible state, not per-draw-call snapshots:
# vkUpdateDescriptorSets on a set a not-yet-submitted command buffer already
# recorded a bind for changes what THAT bind sees once the buffer actually
# executes (Present is the only vkQueueSubmit; a whole frame's worth of
# BeginScene/DrawPrimitive calls get recorded before that). Confirmed live:
# reusing one shared descriptor set and rewriting it on every SetTexture()
# meant every draw in a frame sampled whichever texture the LAST SetTexture
# call of that frame happened to bind, not its own -- one real image
# (uploaded and format-converted correctly) still rendered as nothing
# visible. The fix is one descriptor set per texture, allocated once and
# updated only when that texture's own pixels change, with each draw
# binding the correct pre-existing set for its own bound texture.
_MAX_TEXTURE_DESCRIPTOR_SETS = 8192


def create_descriptor_pool_and_set(device, set_layout):
    """Descriptor pool sized for many per-texture sets, plus the one default
    (opaque white, untextured-draw fallback) set allocated up front."""
    import vulkan as vk
    pool_size = vk.VkDescriptorPoolSize(
        type=vk.VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
        descriptorCount=_MAX_TEXTURE_DESCRIPTOR_SETS)
    pool_ci = vk.VkDescriptorPoolCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
        maxSets=_MAX_TEXTURE_DESCRIPTOR_SETS,
        poolSizeCount=1, pPoolSizes=[pool_size],
    )
    pool = vk.vkCreateDescriptorPool(device, pool_ci, None)
    desc_set = allocate_descriptor_set(device, pool, set_layout)
    return pool, desc_set


def allocate_descriptor_set(device, pool, set_layout):
    """Allocate one new descriptor set from the shared pool (does not
    initialize its binding -- call update_descriptor_set after)."""
    import vulkan as vk
    alloc_info = vk.VkDescriptorSetAllocateInfo(
        sType=vk.VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
        descriptorPool=pool, descriptorSetCount=1, pSetLayouts=[set_layout],
    )
    return vk.vkAllocateDescriptorSets(device, alloc_info)[0]


def create_sampler(device):
    """One shared sampler: linear filter, repeat wrap -- matches typical D3D8
    default texture-stage sampler state (games override via SetSamplerState,
    not yet wired -- this is the reasonable default until they are)."""
    import vulkan as vk
    ci = vk.VkSamplerCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO,
        magFilter=vk.VK_FILTER_LINEAR, minFilter=vk.VK_FILTER_LINEAR,
        addressModeU=vk.VK_SAMPLER_ADDRESS_MODE_REPEAT,
        addressModeV=vk.VK_SAMPLER_ADDRESS_MODE_REPEAT,
        addressModeW=vk.VK_SAMPLER_ADDRESS_MODE_REPEAT,
        mipmapMode=vk.VK_SAMPLER_MIPMAP_MODE_NEAREST,
    )
    return vk.vkCreateSampler(device, ci, None)


def _find_memory_type(physical_device, type_bits: int, flags) -> int:
    import vulkan as vk
    props = vk.vkGetPhysicalDeviceMemoryProperties(physical_device)
    for i in range(props.memoryTypeCount):
        if (type_bits & (1 << i)) and (props.memoryTypes[i].propertyFlags & flags) == flags:
            return i
    raise RuntimeError("No suitable Vulkan memory type")


def upload_texture_image(device, physical_device, command_pool, queue,
                          width: int, height: int, bgra_bytes: bytes):
    """Create a VK_FORMAT_B8G8R8A8_UNORM 2D image and upload raw pixel bytes
    into it via a one-shot staging buffer + copy command, submitted and
    waited on synchronously.

    B8G8R8A8 matches the byte order tew already assumes for D3DFMT_A8R8G8B8
    surfaces elsewhere in this package (see _draw_primitive's diffuse-color
    unpacking) -- real DXT/compressed formats are a separate, not-yet-done
    follow-up; this path treats every texture as raw 32bpp BGRA.

    Returns (VkImage, VkDeviceMemory, VkImageView).
    """
    import vulkan as vk

    size = width * height * 4
    buf_ci = vk.VkBufferCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        size=size, usage=vk.VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
        sharingMode=vk.VK_SHARING_MODE_EXCLUSIVE,
    )
    staging_buf = vk.vkCreateBuffer(device, buf_ci, None)
    mem_req = vk.vkGetBufferMemoryRequirements(device, staging_buf)
    host_flags = (vk.VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                  vk.VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)
    staging_mem = vk.vkAllocateMemory(device, vk.VkMemoryAllocateInfo(
        sType=vk.VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        allocationSize=mem_req.size,
        memoryTypeIndex=_find_memory_type(physical_device, mem_req.memoryTypeBits, host_flags),
    ), None)
    vk.vkBindBufferMemory(device, staging_buf, staging_mem, 0)
    mapped = vk.vkMapMemory(device, staging_mem, 0, size, 0)
    import cffi as _cffi
    _cffi.FFI().memmove(mapped, bytes(bgra_bytes[:size]).ljust(size, b"\x00"), size)
    vk.vkUnmapMemory(device, staging_mem)

    img_ci = vk.VkImageCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
        imageType=vk.VK_IMAGE_TYPE_2D,
        format=vk.VK_FORMAT_B8G8R8A8_UNORM,
        extent=vk.VkExtent3D(width, height, 1),
        mipLevels=1, arrayLayers=1,
        samples=vk.VK_SAMPLE_COUNT_1_BIT,
        tiling=vk.VK_IMAGE_TILING_OPTIMAL,
        usage=(vk.VK_IMAGE_USAGE_TRANSFER_DST_BIT | vk.VK_IMAGE_USAGE_SAMPLED_BIT),
        sharingMode=vk.VK_SHARING_MODE_EXCLUSIVE,
        initialLayout=vk.VK_IMAGE_LAYOUT_UNDEFINED,
    )
    image = vk.vkCreateImage(device, img_ci, None)
    img_mem_req = vk.vkGetImageMemoryRequirements(device, image)
    device_flags = vk.VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT
    image_mem = vk.vkAllocateMemory(device, vk.VkMemoryAllocateInfo(
        sType=vk.VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        allocationSize=img_mem_req.size,
        memoryTypeIndex=_find_memory_type(physical_device, img_mem_req.memoryTypeBits, device_flags),
    ), None)
    vk.vkBindImageMemory(device, image, image_mem, 0)

    # One-shot command buffer: UNDEFINED -> TRANSFER_DST -> copy -> SHADER_READ_ONLY
    alloc = vk.VkCommandBufferAllocateInfo(
        sType=vk.VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        commandPool=command_pool, level=vk.VK_COMMAND_BUFFER_LEVEL_PRIMARY,
        commandBufferCount=1,
    )
    cmd = vk.vkAllocateCommandBuffers(device, alloc)[0]
    vk.vkBeginCommandBuffer(cmd, vk.VkCommandBufferBeginInfo(
        sType=vk.VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
        flags=vk.VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT))

    subresource = vk.VkImageSubresourceRange(
        aspectMask=vk.VK_IMAGE_ASPECT_COLOR_BIT,
        baseMipLevel=0, levelCount=1, baseArrayLayer=0, layerCount=1)

    to_transfer = vk.VkImageMemoryBarrier(
        sType=vk.VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
        oldLayout=vk.VK_IMAGE_LAYOUT_UNDEFINED,
        newLayout=vk.VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
        srcQueueFamilyIndex=vk.VK_QUEUE_FAMILY_IGNORED,
        dstQueueFamilyIndex=vk.VK_QUEUE_FAMILY_IGNORED,
        image=image, subresourceRange=subresource,
        srcAccessMask=0, dstAccessMask=vk.VK_ACCESS_TRANSFER_WRITE_BIT,
    )
    vk.vkCmdPipelineBarrier(cmd,
        vk.VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, vk.VK_PIPELINE_STAGE_TRANSFER_BIT,
        0, 0, None, 0, None, 1, [to_transfer])

    region = vk.VkBufferImageCopy(
        bufferOffset=0, bufferRowLength=0, bufferImageHeight=0,
        imageSubresource=vk.VkImageSubresourceLayers(
            aspectMask=vk.VK_IMAGE_ASPECT_COLOR_BIT,
            mipLevel=0, baseArrayLayer=0, layerCount=1),
        imageOffset=vk.VkOffset3D(0, 0, 0),
        imageExtent=vk.VkExtent3D(width, height, 1),
    )
    vk.vkCmdCopyBufferToImage(cmd, staging_buf, image,
        vk.VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, [region])

    to_shader_read = vk.VkImageMemoryBarrier(
        sType=vk.VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
        oldLayout=vk.VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
        newLayout=vk.VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
        srcQueueFamilyIndex=vk.VK_QUEUE_FAMILY_IGNORED,
        dstQueueFamilyIndex=vk.VK_QUEUE_FAMILY_IGNORED,
        image=image, subresourceRange=subresource,
        srcAccessMask=vk.VK_ACCESS_TRANSFER_WRITE_BIT,
        dstAccessMask=vk.VK_ACCESS_SHADER_READ_BIT,
    )
    vk.vkCmdPipelineBarrier(cmd,
        vk.VK_PIPELINE_STAGE_TRANSFER_BIT, vk.VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
        0, 0, None, 0, None, 1, [to_shader_read])

    vk.vkEndCommandBuffer(cmd)
    submit = vk.VkSubmitInfo(sType=vk.VK_STRUCTURE_TYPE_SUBMIT_INFO,
                              commandBufferCount=1, pCommandBuffers=[cmd])
    vk.vkQueueSubmit(queue, 1, [submit], None)
    vk.vkQueueWaitIdle(queue)
    vk.vkFreeCommandBuffers(device, command_pool, 1, [cmd])

    vk.vkDestroyBuffer(device, staging_buf, None)
    vk.vkFreeMemory(device, staging_mem, None)

    view_ci = vk.VkImageViewCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,
        image=image, viewType=vk.VK_IMAGE_VIEW_TYPE_2D,
        format=vk.VK_FORMAT_B8G8R8A8_UNORM,
        components=vk.VkComponentMapping(
            r=vk.VK_COMPONENT_SWIZZLE_IDENTITY, g=vk.VK_COMPONENT_SWIZZLE_IDENTITY,
            b=vk.VK_COMPONENT_SWIZZLE_IDENTITY, a=vk.VK_COMPONENT_SWIZZLE_IDENTITY),
        subresourceRange=subresource,
    )
    view = vk.vkCreateImageView(device, view_ci, None)
    return image, image_mem, view


def update_descriptor_set(device, desc_set, sampler, image_view):
    import vulkan as vk
    image_info = vk.VkDescriptorImageInfo(
        sampler=sampler, imageView=image_view,
        imageLayout=vk.VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
    )
    write = vk.VkWriteDescriptorSet(
        sType=vk.VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
        dstSet=desc_set, dstBinding=0, dstArrayElement=0,
        descriptorCount=1, descriptorType=vk.VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
        pImageInfo=[image_info],
    )
    vk.vkUpdateDescriptorSets(device, 1, [write], 0, None)


def create_pipeline(device, render_pass, descriptor_set_layout):
    """Create the graphics pipeline for XYZRHW + DIFFUSE + TEX1 geometry.

    Returns (VkPipeline, VkPipelineLayout).
    """
    import vulkan as vk

    # Shader modules from embedded SPIR-V
    def _make_module(spv: bytes):
        ci = vk.VkShaderModuleCreateInfo(
            sType=vk.VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
            codeSize=len(spv),
            pCode=spv,
        )
        return vk.vkCreateShaderModule(device, ci, None)

    vert_mod = _make_module(VERT_SPV)
    frag_mod = _make_module(FRAG_SPV)

    stages = [
        vk.VkPipelineShaderStageCreateInfo(
            sType=vk.VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
            stage=vk.VK_SHADER_STAGE_VERTEX_BIT,
            module=vert_mod,
            pName="main",
        ),
        vk.VkPipelineShaderStageCreateInfo(
            sType=vk.VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
            stage=vk.VK_SHADER_STAGE_FRAGMENT_BIT,
            module=frag_mod,
            pName="main",
        ),
    ]

    # Vertex input: binding 0, stride 40 — pos vec4, color vec4, uv vec2
    binding = vk.VkVertexInputBindingDescription(
        binding=0,
        stride=40,
        inputRate=vk.VK_VERTEX_INPUT_RATE_VERTEX,
    )
    attrs = [
        vk.VkVertexInputAttributeDescription(
            location=0, binding=0,
            format=vk.VK_FORMAT_R32G32B32A32_SFLOAT, offset=0),   # NDC pos
        vk.VkVertexInputAttributeDescription(
            location=1, binding=0,
            format=vk.VK_FORMAT_R32G32B32A32_SFLOAT, offset=16),  # BGRA color
        vk.VkVertexInputAttributeDescription(
            location=2, binding=0,
            format=vk.VK_FORMAT_R32G32_SFLOAT, offset=32),        # UV
    ]
    vert_input = vk.VkPipelineVertexInputStateCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO,
        vertexBindingDescriptionCount=1,
        pVertexBindingDescriptions=[binding],
        vertexAttributeDescriptionCount=len(attrs),
        pVertexAttributeDescriptions=attrs,
    )

    input_assembly = vk.VkPipelineInputAssemblyStateCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO,
        topology=vk.VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST,
        primitiveRestartEnable=vk.VK_FALSE,
    )

    # Viewport and scissor are dynamic — set per draw call
    dynamic_states = [vk.VK_DYNAMIC_STATE_VIEWPORT, vk.VK_DYNAMIC_STATE_SCISSOR]
    dynamic = vk.VkPipelineDynamicStateCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO,
        dynamicStateCount=len(dynamic_states),
        pDynamicStates=dynamic_states,
    )
    viewport_state = vk.VkPipelineViewportStateCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO,
        viewportCount=1,
        scissorCount=1,
    )

    rasterizer = vk.VkPipelineRasterizationStateCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO,
        depthClampEnable=vk.VK_FALSE,
        rasterizerDiscardEnable=vk.VK_FALSE,
        polygonMode=vk.VK_POLYGON_MODE_FILL,
        cullMode=vk.VK_CULL_MODE_NONE,
        frontFace=vk.VK_FRONT_FACE_CLOCKWISE,
        depthBiasEnable=vk.VK_FALSE,
        lineWidth=1.0,
    )

    multisample = vk.VkPipelineMultisampleStateCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO,
        rasterizationSamples=vk.VK_SAMPLE_COUNT_1_BIT,
        sampleShadingEnable=vk.VK_FALSE,
    )

    blend_attach = vk.VkPipelineColorBlendAttachmentState(
        blendEnable=vk.VK_FALSE,
        # Alpha deliberately excluded: D3D8 has no concept of "make the
        # window itself translucent" -- diffuse alpha only ever meant
        # something for in-engine blending, which is disabled here. Writing
        # it into the swapchain's alpha channel let the Wayland compositor
        # treat alpha=0 draws (D3DCOLOR values with a zero top byte, seen
        # constantly on real geometry -- e.g. degenerate/invisible glyph
        # quads) as real window transparency, showing the desktop through
        # the game window instead of the intended color. The backbuffer's
        # alpha is set once by Clear (ARGB=0xff000000 -> alpha=1.0) and
        # must stay untouched by every draw after that.
        colorWriteMask=(
            vk.VK_COLOR_COMPONENT_R_BIT |
            vk.VK_COLOR_COMPONENT_G_BIT |
            vk.VK_COLOR_COMPONENT_B_BIT
        ),
    )
    blend = vk.VkPipelineColorBlendStateCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO,
        logicOpEnable=vk.VK_FALSE,
        attachmentCount=1,
        pAttachments=[blend_attach],
        blendConstants=[0.0, 0.0, 0.0, 0.0],
    )

    layout_ci = vk.VkPipelineLayoutCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
        setLayoutCount=1,
        pSetLayouts=[descriptor_set_layout],
        pushConstantRangeCount=0,
    )
    pipeline_layout = vk.vkCreatePipelineLayout(device, layout_ci, None)

    pipeline_ci = vk.VkGraphicsPipelineCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO,
        stageCount=len(stages),
        pStages=stages,
        pVertexInputState=vert_input,
        pInputAssemblyState=input_assembly,
        pViewportState=viewport_state,
        pRasterizationState=rasterizer,
        pMultisampleState=multisample,
        pColorBlendState=blend,
        pDynamicState=dynamic,
        layout=pipeline_layout,
        renderPass=render_pass,
        subpass=0,
    )
    pipelines = vk.vkCreateGraphicsPipelines(device, None, 1, [pipeline_ci], None)
    pipeline = pipelines[0]

    # Shader modules are no longer needed after pipeline creation
    vk.vkDestroyShaderModule(device, vert_mod, None)
    vk.vkDestroyShaderModule(device, frag_mod, None)

    return pipeline, pipeline_layout


def create_vertex_buffer(device, physical_device, size: int):
    """Allocate a host-visible, persistently-mapped Vulkan vertex buffer.

    Returns (VkBuffer, VkDeviceMemory, ctypes.c_void_p mapped pointer).
    The buffer stays mapped for the lifetime of the device.
    """
    import vulkan as vk

    buf_ci = vk.VkBufferCreateInfo(
        sType=vk.VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        size=size,
        usage=vk.VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
        sharingMode=vk.VK_SHARING_MODE_EXCLUSIVE,
    )
    buf = vk.vkCreateBuffer(device, buf_ci, None)

    mem_req = vk.vkGetBufferMemoryRequirements(device, buf)
    mem_props = vk.vkGetPhysicalDeviceMemoryProperties(physical_device)

    # Find HOST_VISIBLE | HOST_COHERENT memory type
    needed_flags = (
        vk.VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
        vk.VK_MEMORY_PROPERTY_HOST_COHERENT_BIT
    )
    mem_type_idx = -1
    for i in range(mem_props.memoryTypeCount):
        if (mem_req.memoryTypeBits & (1 << i)) and \
           (mem_props.memoryTypes[i].propertyFlags & needed_flags) == needed_flags:
            mem_type_idx = i
            break

    if mem_type_idx < 0:
        logger.error("d3d8", "create_vertex_buffer: no HOST_VISIBLE|HOST_COHERENT memory type")
        raise RuntimeError("No suitable Vulkan memory type for vertex buffer")

    alloc_info = vk.VkMemoryAllocateInfo(
        sType=vk.VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        allocationSize=mem_req.size,
        memoryTypeIndex=mem_type_idx,
    )
    mem = vk.vkAllocateMemory(device, alloc_info, None)
    vk.vkBindBufferMemory(device, buf, mem, 0)

    mapped = vk.vkMapMemory(device, mem, 0, size, 0)
    return buf, mem, mapped


def init_pipeline(device, physical_device, images: list,
                  color_format: int, width: int, height: int,
                  command_pool, queue):
    """Create all pipeline resources after swapchain creation.

    Returns a dict with keys: image_views, render_pass, framebuffers,
    pipeline, pipeline_layout, vertex_buffer, vertex_memory, vertex_mapped,
    descriptor_set_layout, descriptor_pool, descriptor_set, sampler,
    default_tex_image, default_tex_memory, default_tex_view.
    """
    image_views = create_image_views(device, images, color_format)
    render_pass = create_render_pass(device, color_format)
    framebuffers = create_framebuffers(device, render_pass, image_views, width, height)
    descriptor_set_layout = create_descriptor_set_layout(device)
    pipeline, layout = create_pipeline(device, render_pass, descriptor_set_layout)
    VERTEX_BUFFER_SIZE = 4 * 1024 * 1024
    buf, mem, mapped = create_vertex_buffer(device, physical_device, VERTEX_BUFFER_SIZE)

    descriptor_pool, descriptor_set = create_descriptor_pool_and_set(device, descriptor_set_layout)
    sampler = create_sampler(device)
    # 1x1 opaque white -- the default binding for untextured draws (SetTexture
    # cleared to NULL, or before any texture is ever bound), so the shader can
    # unconditionally sample without a branch: white * diffuse == diffuse.
    default_tex_image, default_tex_memory, default_tex_view = upload_texture_image(
        device, physical_device, command_pool, queue, 1, 1, b"\xff\xff\xff\xff")
    update_descriptor_set(device, descriptor_set, sampler, default_tex_view)

    logger.info("d3d8", f"Pipeline ready: {len(image_views)} image views, "
                f"render_pass, {len(framebuffers)} framebuffers")
    return {
        "image_views":      image_views,
        "render_pass":      render_pass,
        "framebuffers":     framebuffers,
        "pipeline":         pipeline,
        "pipeline_layout":  layout,
        "vertex_buffer":    buf,
        "vertex_memory":    mem,
        "vertex_mapped":    mapped,
        "vertex_buffer_size": VERTEX_BUFFER_SIZE,
        "descriptor_set_layout": descriptor_set_layout,
        "descriptor_pool":       descriptor_pool,
        "descriptor_set":        descriptor_set,
        "sampler":               sampler,
        "default_tex_image":     default_tex_image,
        "default_tex_memory":    default_tex_memory,
        "default_tex_view":      default_tex_view,
    }
