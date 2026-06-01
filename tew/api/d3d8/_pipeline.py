"""Vulkan render pass, framebuffer, pipeline, and vertex buffer setup for DrawPrimitive.

Shaders are encoded as SPIR-V binary (no external compiler required).
Vertex format uploaded to Vulkan: 32 bytes per vertex:
  [0]  X_ndc  f32   = (screen_x / vp_w) * 2 - 1
  [4]  Y_ndc  f32   = 1 - (screen_y / vp_h) * 2
  [8]  Z      f32
  [12] W      f32   = 1.0
  [16] B      f32   = (diffuse >>  0 & 0xFF) / 255
  [20] G      f32   = (diffuse >>  8 & 0xFF) / 255
  [24] R      f32   = (diffuse >> 16 & 0xFF) / 255
  [28] A      f32   = (diffuse >> 24 & 0xFF) / 255
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


# ── Passthrough vertex shader ─────────────────────────────────────────────────
# Inputs:  location 0 = vec4 pos (NDC xyzw), location 1 = vec4 color (BGRA floats)
# Outputs: gl_Position = pos, location 0 = color
#
# ID assignments:
#  1=void  2=float  3=vec4  4=fn_type  5=main  6=ptr_in_v4  7=ptr_out_v4
#  8=gl_PerVertex  9=ptr_out_pv  10=in_pos  11=in_color  12=out_color
#  13=gl_pervert  14=int32  15=c0  16=entry  17=pos  18=col  19=gl_pos_chain
#  bound=20
_VERT_WORDS: list[int] = [
    # SPIR-V header
    0x07230203, 0x00010300, 0x00000000, 20, 0x00000000,
    # OpCapability Shader
    0x00020011, 1,
    # OpMemoryModel Logical GLSL450
    0x0003000E, 0, 1,
    # OpEntryPoint Vertex %main "main"  %in_pos %in_color %out_color %gl_pervert
    0x0009000F, 0, 5, 0x6e69616d, 0x00000000, 10, 11, 12, 13,
    # OpDecorate %in_pos    Location 0
    0x00040047, 10, 30, 0,
    # OpDecorate %in_color  Location 1
    0x00040047, 11, 30, 1,
    # OpDecorate %out_color Location 0
    0x00040047, 12, 30, 0,
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
    # %in_pos(10)    = OpVariable %ptr_in_v4(6) Input
    0x0004003B, 6, 10, 1,
    # %in_color(11)  = OpVariable %ptr_in_v4(6) Input
    0x0004003B, 6, 11, 1,
    # %out_color(12) = OpVariable %ptr_out_v4(7) Output
    0x0004003B, 7, 12, 3,
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
    # %gl_pos_chain(19) = OpAccessChain %ptr_out_v4(7) %gl_pervert(13) %c0(15)
    0x00050041, 7, 19, 13, 15,
    # OpStore %gl_pos_chain(19) %pos(17)
    0x0003003E, 19, 17,
    # OpStore %out_color(12) %col(18)
    0x0003003E, 12, 18,
    # OpReturn
    0x000100FD,
    # OpFunctionEnd
    0x00010038,
]
VERT_SPV: bytes = _encode_spv(_VERT_WORDS)


# ── Passthrough fragment shader ───────────────────────────────────────────────
# Input:  location 0 = vec4 color
# Output: location 0 = color (→ framebuffer)
#
# ID assignments:
#  1=void  2=float  3=vec4  4=fn_type  5=main  6=ptr_in_v4  7=ptr_out_v4
#  8=in_color  9=out_color  10=entry  11=col
#  bound=12
_FRAG_WORDS: list[int] = [
    # SPIR-V header
    0x07230203, 0x00010300, 0x00000000, 12, 0x00000000,
    # OpCapability Shader
    0x00020011, 1,
    # OpMemoryModel Logical GLSL450
    0x0003000E, 0, 1,
    # OpEntryPoint Fragment(4) %main(5) "main"  %in_color(8) %out_color(9)
    0x0007000F, 4, 5, 0x6e69616d, 0x00000000, 8, 9,
    # OpExecutionMode %main(5) OriginUpperLeft(7)
    0x00030010, 5, 7,
    # OpDecorate %in_color(8)  Location 0
    0x00040047, 8, 30, 0,
    # OpDecorate %out_color(9) Location 0
    0x00040047, 9, 30, 0,
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
    # %in_color(8)  = OpVariable %ptr_in_v4(6) Input
    0x0004003B, 6, 8, 1,
    # %out_color(9) = OpVariable %ptr_out_v4(7) Output
    0x0004003B, 7, 9, 3,
    # %main(5) = OpFunction %void(1) None %fn_type(4)
    0x00050036, 1, 5, 0, 4,
    # %entry(10) = OpLabel
    0x000200F8, 10,
    # %col(11) = OpLoad %vec4(3) %in_color(8)
    0x0004003D, 3, 11, 8,
    # OpStore %out_color(9) %col(11)
    0x0003003E, 9, 11,
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


def create_pipeline(device, render_pass):
    """Create the graphics pipeline for XYZRHW + DIFFUSE geometry.

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

    # Vertex input: binding 0, stride 32 — two vec4 attributes
    binding = vk.VkVertexInputBindingDescription(
        binding=0,
        stride=32,
        inputRate=vk.VK_VERTEX_INPUT_RATE_VERTEX,
    )
    attrs = [
        vk.VkVertexInputAttributeDescription(
            location=0, binding=0,
            format=vk.VK_FORMAT_R32G32B32A32_SFLOAT, offset=0),   # NDC pos
        vk.VkVertexInputAttributeDescription(
            location=1, binding=0,
            format=vk.VK_FORMAT_R32G32B32A32_SFLOAT, offset=16),  # BGRA color
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
        colorWriteMask=(
            vk.VK_COLOR_COMPONENT_R_BIT |
            vk.VK_COLOR_COMPONENT_G_BIT |
            vk.VK_COLOR_COMPONENT_B_BIT |
            vk.VK_COLOR_COMPONENT_A_BIT
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
        setLayoutCount=0,
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
                  color_format: int, width: int, height: int):
    """Create all pipeline resources after swapchain creation.

    Returns a dict with keys: image_views, render_pass, framebuffers,
    pipeline, pipeline_layout, vertex_buffer, vertex_memory, vertex_mapped.
    """
    image_views = create_image_views(device, images, color_format)
    render_pass = create_render_pass(device, color_format)
    framebuffers = create_framebuffers(device, render_pass, image_views, width, height)
    pipeline, layout = create_pipeline(device, render_pass)
    buf, mem, mapped = create_vertex_buffer(device, physical_device, 4 * 1024 * 1024)
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
    }
