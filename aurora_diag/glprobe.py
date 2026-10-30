# SPDX-License-Identifier: MIT
"""Headless OpenGL and OpenGL ES report through EGL (ctypes, stdlib only).

Run as a separate process by gpu.py (python3 glprobe.py [platform]) so a
driver crash or hang can never take the tool down. It prints one JSON
object on stdout.

It opens an EGL display on the surfaceless platform (or GBM on a render
node, or the default display), creates one context per API without any
surface (EGL_KHR_surfaceless_context), and reads vendor, renderer,
version, profile, GLSL version, every extension and a set of limits. It
never draws anything.
"""

import ctypes
import ctypes.util
import glob
import json
import os
import sys

EGL_NONE = 0x3038
EGL_VENDOR, EGL_VERSION, EGL_EXTENSIONS, EGL_CLIENT_APIS = 0x3053, 0x3054, 0x3055, 0x308D
EGL_PLATFORM_SURFACELESS_MESA = 0x31DD
EGL_PLATFORM_GBM_KHR = 0x31D7
EGL_OPENGL_API, EGL_OPENGL_ES_API = 0x30A2, 0x30A0
EGL_CONTEXT_MAJOR_VERSION, EGL_CONTEXT_MINOR_VERSION = 0x3098, 0x30FB
EGL_CONTEXT_OPENGL_PROFILE_MASK = 0x30FD
EGL_CORE_BIT = 0x1
EGL_RENDERABLE_TYPE, EGL_SURFACE_TYPE = 0x3040, 0x3033
EGL_OPENGL_BIT, EGL_OPENGL_ES2_BIT, EGL_OPENGL_ES3_BIT = 0x8, 0x4, 0x40
EGL_DRM_DEVICE_FILE_EXT = 0x3233
EGL_DEVICE_EXT = 0x322C

GL_VENDOR, GL_RENDERER, GL_VERSION, GL_EXTENSIONS = 0x1F00, 0x1F01, 0x1F02, 0x1F03
GL_SHADING_LANGUAGE_VERSION = 0x8B8C
GL_NUM_EXTENSIONS = 0x821D
GL_CONTEXT_PROFILE_MASK = 0x9126
GL_CONTEXT_FLAGS = 0x821E
GL_NUM_SHADING_LANGUAGE_VERSIONS = 0x82E9

# name: (enum, count, type) for glGetIntegerv / glGetFloatv
LIMITS = {
    "MAX_TEXTURE_SIZE": (0x0D33, 1, "i"),
    "MAX_3D_TEXTURE_SIZE": (0x8073, 1, "i"),
    "MAX_CUBE_MAP_TEXTURE_SIZE": (0x851C, 1, "i"),
    "MAX_ARRAY_TEXTURE_LAYERS": (0x88FF, 1, "i"),
    "MAX_RENDERBUFFER_SIZE": (0x84E8, 1, "i"),
    "MAX_VIEWPORT_DIMS": (0x0D3A, 2, "i"),
    "MAX_SAMPLES": (0x8D57, 1, "i"),
    "MAX_COLOR_ATTACHMENTS": (0x8CDF, 1, "i"),
    "MAX_DRAW_BUFFERS": (0x8824, 1, "i"),
    "MAX_VERTEX_ATTRIBS": (0x8869, 1, "i"),
    "MAX_VARYING_COMPONENTS": (0x8B4B, 1, "i"),
    "MAX_TEXTURE_IMAGE_UNITS": (0x8872, 1, "i"),
    "MAX_COMBINED_TEXTURE_IMAGE_UNITS": (0x8B4D, 1, "i"),
    "MAX_UNIFORM_BLOCK_SIZE": (0x8A30, 1, "i"),
    "MAX_UNIFORM_BUFFER_BINDINGS": (0x8A2F, 1, "i"),
    "MAX_SHADER_STORAGE_BUFFER_BINDINGS": (0x90DD, 1, "i"),
    "MAX_SHADER_STORAGE_BLOCK_SIZE": (0x90DE, 1, "i"),
    "MAX_IMAGE_UNITS": (0x8F38, 1, "i"),
    "MAX_COMPUTE_WORK_GROUP_INVOCATIONS": (0x90EB, 1, "i"),
    "MAX_COMPUTE_SHARED_MEMORY_SIZE": (0x8262, 1, "i"),
    "MAX_GEOMETRY_OUTPUT_VERTICES": (0x8DE0, 1, "i"),
    "MAX_TESS_GEN_LEVEL": (0x8E7E, 1, "i"),
    "MAX_TRANSFORM_FEEDBACK_BUFFERS": (0x8E70, 1, "i"),
    "MAX_TEXTURE_BUFFER_SIZE": (0x8C2B, 1, "i"),
    "MAX_TEXTURE_LOD_BIAS": (0x84FD, 1, "f"),
    "MAX_TEXTURE_MAX_ANISOTROPY": (0x84FF, 1, "f"),
    "ALIASED_LINE_WIDTH_RANGE": (0x846E, 2, "f"),
    "ALIASED_POINT_SIZE_RANGE": (0x846D, 2, "f"),
}


def load(name):
    path = ctypes.util.find_library(name)
    for cand in ([path] if path else []) + ["lib%s.so.1" % name, "lib%s.so" % name]:
        try:
            return ctypes.CDLL(cand)
        except OSError:
            continue
    return None


class Probe:
    def __init__(self):
        self.egl = load("EGL")
        if self.egl is None:
            raise RuntimeError("libEGL not found")
        e = self.egl
        e.eglGetProcAddress.restype = ctypes.c_void_p
        e.eglGetProcAddress.argtypes = [ctypes.c_char_p]
        e.eglQueryString.restype = ctypes.c_char_p
        e.eglQueryString.argtypes = [ctypes.c_void_p, ctypes.c_int]
        e.eglInitialize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        e.eglGetDisplay.restype = ctypes.c_void_p
        e.eglGetDisplay.argtypes = [ctypes.c_void_p]
        e.eglBindAPI.argtypes = [ctypes.c_uint]
        e.eglCreateContext.restype = ctypes.c_void_p
        e.eglCreateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        e.eglMakeCurrent.argtypes = [ctypes.c_void_p] * 4
        e.eglDestroyContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        e.eglTerminate.argtypes = [ctypes.c_void_p]
        e.eglChooseConfig.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_void_p),
                                      ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        e.eglGetError.restype = ctypes.c_int
        self.gbm = None

    def proc(self, name, restype, argtypes):
        addr = self.egl.eglGetProcAddress(name.encode())
        if not addr:
            return None
        return ctypes.CFUNCTYPE(restype, *argtypes)(addr)

    def display(self, platform):
        get_platform = self.proc("eglGetPlatformDisplayEXT", ctypes.c_void_p,
                                 [ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)])
        if platform == "surfaceless":
            if not get_platform:
                raise RuntimeError("no eglGetPlatformDisplayEXT")
            return get_platform(EGL_PLATFORM_SURFACELESS_MESA, None, None), None
        if platform == "gbm":
            self.gbm = load("gbm")
            if not get_platform or not self.gbm:
                raise RuntimeError("no GBM platform")
            nodes = sorted(glob.glob("/dev/dri/renderD*"))
            if not nodes:
                raise RuntimeError("no render node")
            fd = os.open(nodes[0], os.O_RDWR | os.O_CLOEXEC)
            self.gbm.gbm_create_device.restype = ctypes.c_void_p
            self.gbm.gbm_create_device.argtypes = [ctypes.c_int]
            dev = self.gbm.gbm_create_device(fd)
            return get_platform(EGL_PLATFORM_GBM_KHR, ctypes.c_void_p(dev), None), nodes[0]
        return self.egl.eglGetDisplay(None), None

    def gl_info(self, api):
        GLenum = ctypes.c_uint
        get_string = self.proc("glGetString", ctypes.c_char_p, [GLenum])
        get_stringi = self.proc("glGetStringi", ctypes.c_char_p, [GLenum, ctypes.c_uint])
        get_int = self.proc("glGetIntegerv", None, [GLenum, ctypes.POINTER(ctypes.c_int)])
        get_float = self.proc("glGetFloatv", None, [GLenum, ctypes.POINTER(ctypes.c_float)])
        get_error = self.proc("glGetError", ctypes.c_uint, [])

        def s(e):
            v = get_string(e)
            return v.decode("utf-8", "replace") if v else None

        def ints(e, n=1):
            buf = (ctypes.c_int * 4)()
            get_error()
            get_int(e, buf)
            if get_error() != 0:
                return None
            return buf[0] if n == 1 else list(buf[:n])

        def floats(e, n=1):
            buf = (ctypes.c_float * 4)()
            get_error()
            get_float(e, buf)
            if get_error() != 0:
                return None
            return round(buf[0], 4) if n == 1 else [round(x, 4) for x in buf[:n]]

        info = {"vendor": s(GL_VENDOR), "renderer": s(GL_RENDERER), "version": s(GL_VERSION),
                "glsl_version": s(GL_SHADING_LANGUAGE_VERSION)}
        exts = []
        n = ints(GL_NUM_EXTENSIONS) if get_stringi else None
        if n:
            for i in range(n):
                v = get_stringi(GL_EXTENSIONS, i)
                if v:
                    exts.append(v.decode())
        else:
            exts = (s(GL_EXTENSIONS) or "").split()
        info["extensions"] = sorted(exts)
        info["extension_count"] = len(exts)
        if api == "gl":
            mask = ints(GL_CONTEXT_PROFILE_MASK)
            info["profile"] = {1: "core", 2: "compatibility"}.get(mask, "compatibility" if mask in (None, 0) else str(mask))
        else:
            info["profile"] = "es"
        limits = {}
        for name, (enum, cnt, typ) in LIMITS.items():
            v = ints(enum, cnt) if typ == "i" else floats(enum, cnt)
            if v is not None:
                limits[name] = v
        info["limits"] = limits
        r = (info["renderer"] or "").lower()
        info["path"] = "zink" if "zink" in r else ("llvmpipe" if ("llvmpipe" in r or "softpipe" in r) else "native")
        return info

    def context(self, dpy, api, attribs, cfg):
        self.egl.eglBindAPI(EGL_OPENGL_API if api == "gl" else EGL_OPENGL_ES_API)
        arr = (ctypes.c_int * (len(attribs) + 1))(*(attribs + [EGL_NONE]))
        ctx = self.egl.eglCreateContext(dpy, cfg, None, arr)
        if not ctx:
            return None
        if not self.egl.eglMakeCurrent(dpy, None, None, ctx):
            self.egl.eglDestroyContext(dpy, ctx)
            return None
        try:
            return self.gl_info(api)
        finally:
            self.egl.eglMakeCurrent(dpy, None, None, None)
            self.egl.eglDestroyContext(dpy, ctx)

    def config(self, dpy, exts, renderable):
        if "EGL_KHR_no_config_context" in exts:
            return None
        attrs = (ctypes.c_int * 5)(EGL_RENDERABLE_TYPE, renderable, EGL_SURFACE_TYPE, 0, EGL_NONE)
        cfg = ctypes.c_void_p()
        n = ctypes.c_int()
        if self.egl.eglChooseConfig(dpy, attrs, ctypes.byref(cfg), 1, ctypes.byref(n)) and n.value:
            return cfg
        return None

    def run(self, platform):
        dpy, node = self.display(platform)
        if not dpy:
            raise RuntimeError("no EGL display (error 0x%x)" % self.egl.eglGetError())
        maj, mn = ctypes.c_int(), ctypes.c_int()
        if not self.egl.eglInitialize(dpy, ctypes.byref(maj), ctypes.byref(mn)):
            raise RuntimeError("eglInitialize failed (error 0x%x)" % self.egl.eglGetError())
        q = lambda e: (self.egl.eglQueryString(dpy, e) or b"").decode()
        exts = q(EGL_EXTENSIONS).split()
        out = {"platform": platform, "render_node": node,
               "egl": {"version": "%d.%d" % (maj.value, mn.value), "vendor": q(EGL_VENDOR),
                       "driver_version": q(EGL_VERSION), "client_apis": q(EGL_CLIENT_APIS),
                       "extension_count": len(exts), "extensions": sorted(exts)}}
        drm = self._drm_device(dpy, exts)
        if drm:
            out["egl"]["drm_device"] = drm
        # Desktop GL: the highest core profile first, then compatibility.
        core = None
        for major, minor in ((4, 6), (4, 5), (4, 3), (4, 1), (4, 0), (3, 3), (3, 2)):
            core = self.context(dpy, "gl", [EGL_CONTEXT_MAJOR_VERSION, major, EGL_CONTEXT_MINOR_VERSION, minor,
                                            EGL_CONTEXT_OPENGL_PROFILE_MASK, EGL_CORE_BIT],
                                self.config(dpy, exts, EGL_OPENGL_BIT))
            if core:
                break
        out["gl_core"] = core
        out["gl_compat"] = self.context(dpy, "gl", [], self.config(dpy, exts, EGL_OPENGL_BIT))
        es = None
        for major, minor in ((3, 2), (3, 1), (3, 0), (2, 0)):
            es = self.context(dpy, "gles", [EGL_CONTEXT_MAJOR_VERSION, major, EGL_CONTEXT_MINOR_VERSION, minor],
                              self.config(dpy, exts, EGL_OPENGL_ES3_BIT if major >= 3 else EGL_OPENGL_ES2_BIT))
            if es:
                break
        out["gles"] = es
        self.egl.eglTerminate(dpy)
        return out

    def _drm_device(self, dpy, exts):
        """The DRM device file behind the display (EGL_EXT_device_query)."""
        qda = self.proc("eglQueryDisplayAttribEXT", ctypes.c_uint,
                        [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)])
        qds = self.proc("eglQueryDeviceStringEXT", ctypes.c_char_p, [ctypes.c_void_p, ctypes.c_int])
        if not qda or not qds:
            return None
        dev = ctypes.c_void_p()
        if not qda(dpy, EGL_DEVICE_EXT, ctypes.byref(dev)) or not dev.value:
            return None
        v = qds(dev, EGL_DRM_DEVICE_FILE_EXT)
        return v.decode() if v else None


def main():
    platforms = sys.argv[1:] or ["surfaceless", "gbm"]
    errors = {}
    for p in platforms:
        try:
            out = Probe().run(p)
            out["errors"] = errors
            print(json.dumps(out))
            return 0
        except Exception as e:  # try the next platform
            errors[p] = str(e)
    print(json.dumps({"error": "no EGL platform worked", "errors": errors}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
