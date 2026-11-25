"""ctypes wrapper around libmpv's OpenGL render API.

The context owns one ``mpv_render_context`` for the lifetime of the GL
context that created it:

* create it only after Qt reports the GL context as current;
* call :meth:`RenderContext.update` / :meth:`RenderContext.render` on the
  GUI thread that owns the GL context, at most once per repaint driver;
* call :meth:`RenderContext.free` before the GL context is destroyed.

Every method is defensive after :meth:`free`: the render context is borrowed
by libmpv's GL thread, so using it after teardown is undefined behaviour.
"""
from __future__ import annotations

import ctypes
from collections.abc import Callable
from typing import Any

import mpv

from .state import log

API_TYPE_OPENGL = "opengl"
FLIP_Y = True
DEFAULT_FBO = 0

#: GL proc loader as exposed by QOpenGLContext.getProcAddress().
GetProcAddress = Callable[[str], int | None]

_MpvGlGetProcAddressFn = ctypes.CFUNCTYPE(
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p
)


class RenderError(RuntimeError):
    """libmpv refused to create or drive the render context."""


def _param_array(keepalive: list[object], **kwargs: object) -> Any:
    """Build the NULL-terminated ``mpv_render_param`` array for libmpv.

    python-mpv builds a ``str`` param's ``data`` pointer with
    ``cast(c_char_p(text), c_void_p)`` but keeps no reference to the encoded
    buffer, so libmpv would read freed memory. Re-point that entry at a
    buffer that outlives the call, and pin every entry in ``keepalive``.
    """
    params = mpv.kwargs_to_render_param_array(kwargs)
    keepalive.append(params)
    for entry in params:
        if entry.type_id == 1:
            buffer = ctypes.create_string_buffer(str(kwargs["api_type"]).encode())
            entry.data = ctypes.cast(buffer, ctypes.c_void_p)
            keepalive.append(buffer)
    return params


class RenderContext:
    """One libmpv OpenGL render context (flip-y, viewport-sized).

    ``fbo`` is the GL framebuffer libmpv draws into. It is 0 for a plain
    window framebuffer, but a QOpenGLWidget paints into its own FBO, so such
    callers must pass ``widget.defaultFramebufferObject`` (or the current
    value) or the frames land in the window backbuffer and never composite.
    Pass a callable when the id can change; it is re-read on every frame.
    """

    def __init__(
        self,
        handle: Any,
        get_proc_address: GetProcAddress,
        fbo: int | Callable[[], int] = DEFAULT_FBO,
    ) -> None:
        if handle is None:
            raise RenderError("render context requires an mpv instance")
        self._owner = handle
        self._mpv_handle = getattr(handle, "handle", handle)
        self._get_proc_address = get_proc_address
        self._fbo = fbo
        self._freed = False
        # Keep the trampoline and the create-time structs alive: libmpv
        # stores pointers into them for the whole context lifetime.
        self._create_keepalive: list[object] = []
        self._proc_cb = _MpvGlGetProcAddressFn(self._on_get_proc_address)
        self.create_params = _param_array(
            self._create_keepalive,
            api_type=API_TYPE_OPENGL,
            opengl_init_params={"get_proc_address": self._proc_cb},
            opengl_fbo={"w": 1, "h": 1, "fbo": self._target_fbo()},
            flip_y=FLIP_Y,
        )
        buffer = ctypes.cast(
            ctypes.create_string_buffer(ctypes.sizeof(mpv.MpvRenderCtxHandle)),
            ctypes.POINTER(mpv.MpvRenderCtxHandle),
        )
        try:
            # python-mpv's errcheck raises on failure and yields None on
            # success, so the only signal here is "no exception".
            mpv._mpv_render_context_create(
                buffer, self._mpv_handle, self.create_params
            )
        except Exception as exc:
            raise RenderError(f"could not create render context: {exc}") from exc
        self._handle = buffer.contents

    @property
    def handle(self) -> Any:
        """Underlying ``mpv_render_context`` handle."""
        return self._handle

    @property
    def freed(self) -> bool:
        """Whether :meth:`free` already ran."""
        return self._freed

    def _on_get_proc_address(
        self, _context: ctypes.c_void_p, name: bytes
    ) -> int | None:
        try:
            return self._get_proc_address(name.decode("ascii"))
        except Exception as exc:  # noqa: BLE001 - GL loader must not unwind
            log.debug("GL proc loader failed for %r: %s", name, exc)
            return None

    def update(self) -> bool:
        """Ask libmpv whether a new frame is ready; no-op after free."""
        if self._freed:
            return False
        return bool(mpv._mpv_render_context_update(self._handle) & 1)

    def _target_fbo(self) -> int:
        """Resolve the framebuffer id for the current frame."""
        target = self._fbo
        if callable(target):
            try:
                target = target()
            except Exception as exc:  # noqa: BLE001 - never abort a paint
                log.debug("FBO provider failed: %s", exc)
                return DEFAULT_FBO
        return max(0, int(target))

    def render(self, width: int, height: int) -> None:
        """Render one frame into the target FBO for the current viewport."""
        if self._freed:
            return
        # R3: flip-y is unconditional and the target FBO is rebound on every
        # frame with the current widget viewport.
        keepalive: list[object] = []
        params = _param_array(
            keepalive,
            opengl_fbo={
                "w": max(1, int(width)),
                "h": max(1, int(height)),
                "fbo": self._target_fbo(),
            },
            flip_y=FLIP_Y,
        )
        mpv._mpv_render_context_render(self._handle, params)

    def set_parameter(self, name: str, value: object) -> None:
        """Forward a render parameter change to libmpv; no-op after free."""
        if self._freed:
            return
        param = mpv.MpvRenderParam(name, value)
        mpv._mpv_render_context_set_parameter(self._handle, param)

    def free(self) -> None:
        """Release the render context exactly once; safe to call twice."""
        if self._freed:
            return
        self._freed = True
        try:
            mpv._mpv_render_context_free(self._handle)
        except Exception as exc:  # noqa: BLE001 - teardown must not crash
            log.exception("could not free mpv render context: %s", exc)
