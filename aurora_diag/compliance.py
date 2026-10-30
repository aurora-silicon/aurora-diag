# SPDX-License-Identifier: MIT
"""How the GPU drivers measure up to what they claim.

Vulkan: the conformance version the driver reports (VkPhysicalDevice-
Vulkan12Properties.conformanceVersion, 0.0.0.0 means never submitted), and
a check of features the Vulkan specification makes mandatory for the API
version the device claims ("Feature Requirements" chapter). OpenGL and
OpenGL ES have no conformance query; the check compares the limits the
context reports with the minimums in the spec's state tables for the
version claimed.

REQUIRED_* lists are spot checks of the spec, not the complete tables:
docs/COMPLIANCE.md says which rows are covered. A missing entry is a
finding to look at, not proof of non-conformance.
"""

import re

# Features each Vulkan core version makes mandatory (cumulative).
REQUIRED_VK = {
    (1, 1): ["multiview"],
    (1, 2): ["uniformBufferStandardLayout", "subgroupBroadcastDynamicId", "imagelessFramebuffer",
             "separateDepthStencilLayouts", "hostQueryReset", "timelineSemaphore", "shaderSubgroupExtendedTypes"],
    (1, 3): ["robustImageAccess", "inlineUniformBlock", "pipelineCreationCacheControl", "privateData",
             "shaderDemoteToHelperInvocation", "shaderTerminateInvocation", "subgroupSizeControl",
             "computeFullSubgroups", "synchronization2", "shaderZeroInitializeWorkgroupMemory",
             "dynamicRendering", "shaderIntegerDotProduct", "maintenance4", "bufferDeviceAddress",
             "vulkanMemoryModel", "vulkanMemoryModelDeviceScope"],
    (1, 4): ["dynamicRenderingLocalRead", "maintenance5", "maintenance6", "pushDescriptor", "indexTypeUint8",
             "shaderSubgroupRotate", "shaderFloatControls2", "shaderExpectAssume",
             "vertexAttributeInstanceRateDivisor", "bresenhamLines", "pipelineRobustness",
             "fullDrawIndexUint32", "imageCubeArray", "independentBlend", "sampleRateShading",
             "drawIndirectFirstInstance", "depthClamp", "depthBiasClamp", "samplerAnisotropy",
             "fragmentStoresAndAtomics", "shaderImageGatherExtended", "shaderInt16", "largePoints",
             "samplerYcbcrConversion", "storageBuffer16BitAccess", "variablePointers",
             "variablePointersStorageBuffer", "samplerMirrorClampToEdge", "scalarBlockLayout"],
}

# Minimum values from the state tables (GL 4.6 core, ES 3.2).
GL46_MIN = {"MAX_TEXTURE_SIZE": 16384, "MAX_3D_TEXTURE_SIZE": 2048, "MAX_CUBE_MAP_TEXTURE_SIZE": 16384,
            "MAX_ARRAY_TEXTURE_LAYERS": 2048, "MAX_RENDERBUFFER_SIZE": 16384, "MAX_SAMPLES": 4,
            "MAX_COLOR_ATTACHMENTS": 8, "MAX_DRAW_BUFFERS": 8, "MAX_VERTEX_ATTRIBS": 16,
            "MAX_UNIFORM_BLOCK_SIZE": 16384, "MAX_TEXTURE_IMAGE_UNITS": 16,
            "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 80, "MAX_SHADER_STORAGE_BUFFER_BINDINGS": 8,
            "MAX_SHADER_STORAGE_BLOCK_SIZE": 1 << 27, "MAX_IMAGE_UNITS": 8,
            "MAX_COMPUTE_WORK_GROUP_INVOCATIONS": 1024, "MAX_COMPUTE_SHARED_MEMORY_SIZE": 32768,
            "MAX_GEOMETRY_OUTPUT_VERTICES": 256, "MAX_TESS_GEN_LEVEL": 64,
            "MAX_TRANSFORM_FEEDBACK_BUFFERS": 4, "MAX_TEXTURE_BUFFER_SIZE": 65536,
            "MAX_TEXTURE_LOD_BIAS": 2.0, "MAX_TEXTURE_MAX_ANISOTROPY": 16.0}
GLES32_MIN = {"MAX_TEXTURE_SIZE": 2048, "MAX_3D_TEXTURE_SIZE": 256, "MAX_CUBE_MAP_TEXTURE_SIZE": 2048,
              "MAX_ARRAY_TEXTURE_LAYERS": 256, "MAX_RENDERBUFFER_SIZE": 2048, "MAX_SAMPLES": 4,
              "MAX_COLOR_ATTACHMENTS": 4, "MAX_DRAW_BUFFERS": 4, "MAX_VERTEX_ATTRIBS": 16,
              "MAX_UNIFORM_BLOCK_SIZE": 16384, "MAX_TEXTURE_IMAGE_UNITS": 16,
              "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 96, "MAX_SHADER_STORAGE_BLOCK_SIZE": 1 << 27,
              "MAX_COMPUTE_WORK_GROUP_INVOCATIONS": 128, "MAX_COMPUTE_SHARED_MEMORY_SIZE": 16384,
              "MAX_GEOMETRY_OUTPUT_VERTICES": 256, "MAX_TESS_GEN_LEVEL": 64,
              "MAX_TEXTURE_BUFFER_SIZE": 65536, "MAX_TEXTURE_LOD_BIAS": 2.0}


def _ver(s):
    m = re.match(r"(\d+)\.(\d+)", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def vulkan_device(dev):
    """dev: one device dict from gpu.vulkan() (with feature_values)."""
    api = _ver(dev.get("apiVersion"))
    feats = dev.get("feature_values") or {}
    want = []
    for v, names in sorted(REQUIRED_VK.items()):
        if api and v <= api:
            want += names
    missing = [n for n in want if not feats.get(n)]
    cv = dev.get("conformance") or dev.get("conformanceVersion")
    return {"api": dev.get("apiVersion"), "conformance": cv,
            "conformance_submitted": bool(cv) and cv not in ("0.0.0.0", "0.0.0"),
            "required_checked": len(want), "missing": missing}


def gl_context(info, es=False):
    v = info.get("version") or ""
    ver = _ver(re.sub(r"^OpenGL ES ", "", v))
    table = None
    if es and ver and ver >= (3, 2):
        table, claim = GLES32_MIN, "ES 3.2"
    elif not es and ver and ver >= (4, 6):
        table, claim = GL46_MIN, "4.6"
    if table is None:
        return {"claimed": v, "checked": 0, "missing": [], "note": "no minimums table for this version"}
    lim = info.get("limits", {})
    missing = []
    for k, need in table.items():
        have = lim.get(k)
        if have is None or have < need:
            missing.append("%s %s < %s" % (k, have, need))
    return {"claimed": claim, "checked": len(table), "missing": missing}


def summary(vk, gl):
    out = {}
    for d in (vk or {}).get("devices", []):
        if "CPU" in (d.get("deviceType") or ""):
            continue
        out["vulkan:%s" % d.get("deviceName")] = vulkan_device(d)
    for api, es in (("gl_core", False), ("gl_compat", False), ("gles", True)):
        info = (gl or {}).get(api)
        if info:
            out["gl:%s" % api] = gl_context(info, es)
    return out
