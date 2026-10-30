# Compliance checks

These are spot checks that flag something to look at. They do not replace the Khronos conformance test suites.

## Vulkan

- `conformance` is `VkPhysicalDeviceVulkan12Properties::conformanceVersion` as the driver reports it. `0.0.0.0` means the driver does not claim a conformant submission.
- `missing` lists features the specification's Feature Requirements chapter makes mandatory for the API version the device claims, which the device reports as unsupported. The lists in `compliance.py` (`REQUIRED_VK`) cover these rows:
  - 1.1: `multiview`
  - 1.2: `uniformBufferStandardLayout`, `subgroupBroadcastDynamicId`, `imagelessFramebuffer`, `separateDepthStencilLayouts`, `hostQueryReset`, `timelineSemaphore`, `shaderSubgroupExtendedTypes`
  - 1.3: the 1.3 core set (`robustImageAccess`, `inlineUniformBlock`, `synchronization2`, `dynamicRendering`, `maintenance4`, `bufferDeviceAddress`, `vulkanMemoryModel`, ...)
  - 1.4: the 1.4 additions (`maintenance5`, `maintenance6`, `pushDescriptor`, `dynamicRenderingLocalRead`, `indexTypeUint8`, `shaderSubgroupRotate`, `shaderFloatControls2`, ...) and the core features 1.4 made mandatory (`samplerAnisotropy`, `imageCubeArray`, `shaderInt16`, ...)
- Features conditional on another feature or extension are not checked.

The feature values come from `vulkaninfo --json`, merged across every feature structure (a feature counts as present if any structure reports it).

## OpenGL and OpenGL ES

There is no API to ask a GL driver whether it is conformant. The check compares the limits a context reports with the minimum values in the state tables of the version it claims: OpenGL 4.6 core and compatibility (`GL46_MIN`) and OpenGL ES 3.2 (`GLES32_MIN`). Contexts claiming another version are reported as not checked.

## Adding rows

Add the feature or limit to the table in `compliance.py`, cite the spec section in the commit, and add a case to `tests/test_bringup.py`.
