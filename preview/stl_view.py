"""3D model viewer (STL / OBJ / PLY) built on Gtk.GLArea + PyOpenGL (core 3.3).

Rendering: one interleaved VBO (xyz float32 + GL_INT_2_10_10_10_REV normal,
16 bytes/vertex) drawn with a single glDrawArrays; flat shading uses screen
space derivatives, the wireframe overlay is derived from gl_VertexID % 3
(barycentric edges), so neither costs extra memory. The scene is drawn into a
4x MSAA framebuffer, resolved, and blitted into the GLArea's framebuffer.

Interaction (turntable camera, Z up):
  left-drag orbit · Shift/middle/right-drag pan · touchpad two-finger scroll
  orbit (with inertia) · mouse wheel / Ctrl+scroll / pinch zoom ·
  double-click reset · R reset · W wireframe
"""
import os

from .runtime import configure_gl_env

configure_gl_env()

import ctypes
import math
import sys
import threading
import time
import weakref

import numpy as np
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import mesh_io
from .viewer_base import Viewer

try:
    from OpenGL import GL
    _GL_IMPORT_ERROR = None
except Exception as _exc:  # noqa: BLE001  (missing PyOpenGL / libGL)
    GL = None
    _GL_IMPORT_ERROR = _exc

DEBUG = bool(os.environ.get("PREVIEW_DEBUG"))


# one parse at a time: flipping quickly through big files (Quick Look) must
# not run several multi-GB parses concurrently; superseded loads are skipped
_LOAD_LOCK = threading.Lock()


def _weak(method):
    """Signal callback that doesn't keep the viewer alive: widget -> closure
    -> bound method -> viewer cycles are invisible to Python's GC, so strong
    handlers would leak every viewer a long-lived host (Quick Look) swaps out."""
    ref = weakref.WeakMethod(method)

    def callback(*args):
        m = ref()
        return m(*args) if m is not None else False
    return callback


def _log(*a):
    if DEBUG:
        print("[stl_view]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# custom symbolic icons (written once to the user cache, added to the theme)
# --------------------------------------------------------------------------
def _strokes(segments, w=1.25):
    """GTK's symbolic loader fills every path and ignores strokes, so line art
    is emitted as filled quads (consistent winding, nonzero = union) with
    round caps."""
    parts, caps = [], set()
    for (x0, y0), (x1, y1) in segments:
        dx, dy = x1 - x0, y1 - y0
        n = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / n * w / 2, dx / n * w / 2
        parts.append(f"M{x0 + nx:.3f} {y0 + ny:.3f}L{x1 + nx:.3f} {y1 + ny:.3f}"
                     f"L{x1 - nx:.3f} {y1 - ny:.3f}L{x0 - nx:.3f} {y0 - ny:.3f}Z")
        caps.update({(x0, y0), (x1, y1)})
    body = f'<path fill="#000" d="{"".join(parts)}"/>'
    body += "".join(f'<circle fill="#000" cx="{x}" cy="{y}" r="{w / 2}"/>' for x, y in caps)
    return body


def _poly(*pts, closed=True):
    seg = list(zip(pts, pts[1:]))
    if closed:
        seg.append((pts[-1], pts[0]))
    return seg


_C, _T, _R, _BR, _B, _BL, _L = (8, 8), (8, 1.5), (14, 4.8), (14, 11.2), (8, 14.5), (2, 11.2), (2, 4.8)
_ICONS = {
    "preview-model-wireframe-symbolic": _strokes(
        _poly(_T, _R, _BR, _B, _BL, _L) + [(_C, _B), (_C, _R), (_C, _L), (_L, _B)]),
    "preview-model-shading-symbolic":
        '<path fill="#000" fill-rule="evenodd" d="M8 1.4A6.6 6.6 0 1 1 7.99 1.4ZM8 2.7A5.3 5.3 0 1 0 8.01 2.7Z"/>'
        '<path fill="#000" d="M8 2.7A5.3 5.3 0 0 1 8 13.3Z"/>',
    "preview-model-grid-symbolic": _strokes(
        _poly((4.2, 3.5), (11.8, 3.5), (15, 12.5), (1, 12.5))
        + [((2.6, 8), (13.4, 8)), ((6.7, 3.5), (5.6, 12.5)), ((9.3, 3.5), (10.4, 12.5))]),
    "preview-model-views-symbolic":
        '<path fill="#000" opacity="0.45" d="M8 1 14.5 4.5 8 8 1.5 4.5z"/>'
        '<path fill="#000" d="M1.5 5.4 7.5 8.8V15L1.5 11.6z"/>'
        '<path fill="#000" opacity="0.75" d="M14.5 5.4 8.5 8.8V15l6-3.4z"/>',
}


def _install_icons():
    try:
        base = os.path.join(GLib.get_user_cache_dir(), "view", "icons")
        d = os.path.join(base, "hicolor", "scalable", "actions")
        os.makedirs(d, exist_ok=True)
        for name, body in _ICONS.items():
            path = os.path.join(d, name + ".svg")
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
                   f'viewBox="0 0 16 16">{body}\n</svg>\n')
            try:
                with open(path) as fh:
                    if fh.read() == svg:
                        continue
            except OSError:
                pass
            with open(path, "w") as fh:
                fh.write(svg)
        display = Gdk.Display.get_default()
        if display is not None:
            theme = Gtk.IconTheme.get_for_display(display)
            if base not in theme.get_search_path():
                theme.add_search_path(base)
    except Exception as exc:  # noqa: BLE001 - icons are cosmetic
        _log("icon install failed:", exc)


# --------------------------------------------------------------------------
# math helpers (row-major numpy, uploaded with transpose=GL_TRUE)
# --------------------------------------------------------------------------
def _perspective(fovy_deg, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fovy_deg) / 2)
    m = np.zeros((4, 4), np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = 2 * far * near / (near - far)
    m[3, 2] = -1.0
    return m


def _rx(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[1, 0, 0, 0], [0, c, -s, 0], [0, s, c, 0], [0, 0, 0, 1]], np.float64)


def _rz(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, -s, 0, 0], [s, c, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], np.float64)


def _translate(v):
    m = np.eye(4)
    m[:3, 3] = v
    return m


def _srgb_to_linear(c):
    c = np.asarray(c, np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


# --------------------------------------------------------------------------
# shaders
# --------------------------------------------------------------------------
MESH_VS = """
#version 330 core
layout(location = 0) in vec3 a_pos;
layout(location = 1) in vec4 a_nrm;
uniform mat4 u_mv;
uniform mat4 u_proj;
uniform mat3 u_nmat;
out vec3 v_pos;
out vec3 v_nrm;
out vec3 v_bary;
void main() {
    vec4 p = u_mv * vec4(a_pos, 1.0);
    v_pos = p.xyz;
    v_nrm = u_nmat * a_nrm.xyz;
    int c = gl_VertexID % 3;
    v_bary = vec3(c == 0, c == 1, c == 2);
    gl_Position = u_proj * p;
}
"""

MESH_FS = """
#version 330 core
in vec3 v_pos;
in vec3 v_nrm;
in vec3 v_bary;
uniform int u_flat;        // 1 = flat shading (also used until normals arrive)
uniform int u_wire;
uniform float u_px;
uniform vec3 u_base;      // linear
uniform vec3 u_sky;       // linear ambient from above
uniform vec3 u_ground;    // linear ambient from below
uniform vec3 u_up;        // world up in view space
uniform vec3 u_wire_col;  // sRGB
out vec4 frag;
void main() {
    vec3 fn = normalize(cross(dFdx(v_pos), dFdy(v_pos)));   // faces the viewer
    vec3 n = fn;
    if (u_flat == 0) {
        vec3 s = normalize(v_nrm);
        n = dot(s, fn) < 0.0 ? -s : s;      // robust to inverted winding
    }
    vec3 V = normalize(-v_pos);
    vec3 L1 = normalize(vec3(-0.45, 0.70, 0.60));   // key: upper left
    vec3 L2 = normalize(vec3(0.75, -0.10, 0.45));   // fill: right
    float d1 = max(dot(n, L1), 0.0);
    float d2 = max(dot(n, L2), 0.0);
    float s1 = pow(max(dot(n, normalize(L1 + V)), 0.0), 60.0);
    float s2 = pow(max(dot(n, normalize(L2 + V)), 0.0), 30.0);
    float hemi = dot(n, u_up) * 0.5 + 0.5;
    vec3 amb = mix(u_ground, u_sky, hemi);
    vec3 col = u_base * (amb + d1 * vec3(1.00, 0.97, 0.93) * 0.95
                             + d2 * vec3(0.80, 0.88, 1.00) * 0.35)
             + vec3(1.0) * (s1 * 0.30 + s2 * 0.06);
    float rim = pow(1.0 - max(dot(n, V), 0.0), 4.0);
    col += rim * 0.05 * u_sky;
    col = pow(clamp(col, 0.0, 1.0), vec3(1.0 / 2.2));
    if (u_wire == 1) {
        vec3 d = fwidth(v_bary);
        vec3 a = smoothstep(d * (0.45 * u_px), d * (0.45 * u_px + 1.0), v_bary);
        float e = 1.0 - min(min(a.x, a.y), a.z);
        col = mix(col, u_wire_col, e * 0.85);
    }
    frag = vec4(col, 1.0);
}
"""

BG_VS = """
#version 330 core
out vec2 v_uv;
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    v_uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

BG_FS = """
#version 330 core
in vec2 v_uv;
uniform vec3 u_top;
uniform vec3 u_bottom;
out vec4 frag;
float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
void main() {
    float t = smoothstep(0.0, 1.0, v_uv.y);
    vec3 c = mix(u_bottom, u_top, t);
    vec2 q = v_uv - vec2(0.5, 0.55);
    c *= 1.0 - 0.10 * dot(q, q);                       // faint vignette
    c += (hash(gl_FragCoord.xy) - 0.5) / 255.0;          // dither: no banding
    frag = vec4(c, 1.0);
}
"""

GRID_VS = """
#version 330 core
uniform mat4 u_mvp;
uniform float u_extent;
uniform float u_z;
out vec2 v_xy;
void main() {
    vec2 c = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1)) * 2.0 - 1.0;
    v_xy = c * u_extent;
    gl_Position = u_mvp * vec4(v_xy, u_z, 1.0);
}
"""

GRID_FS = """
#version 330 core
in vec2 v_xy;
uniform float u_step;
uniform vec2 u_offset;
uniform float u_extent;
uniform float u_px;
uniform vec4 u_minor;      // sRGB + alpha
uniform vec4 u_major;
uniform vec2 u_shadow;     // footprint half size
uniform float u_shadow_a;
uniform float u_below;     // 1 when looking from underneath
out vec4 frag;
float lines(vec2 coord, float w) {
    vec2 fw = max(fwidth(coord), vec2(1e-6));
    vec2 g = abs(fract(coord - 0.5) - 0.5) / fw;
    float l = min(g.x, g.y);
    return 1.0 - smoothstep(w * 0.5, w * 0.5 + 1.0, l);
}
void main() {
    vec2 c = (v_xy + u_offset) / u_step;
    float dens = clamp(1.6 - 2.5 * max(fwidth(c).x, fwidth(c).y), 0.0, 1.0);
    float mi = lines(c, u_px) * dens;
    float ma = lines(c / 10.0, u_px * 1.2);
    float r = length(v_xy) / u_extent;
    float fade = 1.0 - smoothstep(0.45, 1.0, r);
    float ag = max(mi * u_minor.a, ma * u_major.a) * fade;
    vec3 cg = ma * u_major.a >= mi * u_minor.a ? u_major.rgb : u_minor.rgb;
    vec2 q = abs(v_xy / max(u_shadow, vec2(1e-6)));
    float sd = pow(pow(q.x, 4.0) + pow(q.y, 4.0), 0.25);
    float sh = (1.0 - smoothstep(0.55, 1.45, sd)) * u_shadow_a * (1.0 - u_below);
    float a = ag + sh * (1.0 - ag);
    vec3 rgb = a > 0.0 ? cg * ag / a : vec3(0.0);
    frag = vec4(rgb, a * mix(1.0, 0.35, u_below));
}
"""


def _compile(vs, fs):
    def shader(src, kind):
        s = GL.glCreateShader(kind)
        GL.glShaderSource(s, src)
        GL.glCompileShader(s)
        if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS):
            log = GL.glGetShaderInfoLog(s)
            raise RuntimeError("shader compile failed: " + (log.decode() if isinstance(log, bytes) else str(log)))
        return s
    a, b = shader(vs, GL.GL_VERTEX_SHADER), shader(fs, GL.GL_FRAGMENT_SHADER)
    p = GL.glCreateProgram()
    GL.glAttachShader(p, a)
    GL.glAttachShader(p, b)
    GL.glLinkProgram(p)
    GL.glDeleteShader(a)
    GL.glDeleteShader(b)
    if not GL.glGetProgramiv(p, GL.GL_LINK_STATUS):
        log = GL.glGetProgramInfoLog(p)
        raise RuntimeError("program link failed: " + (log.decode() if isinstance(log, bytes) else str(log)))
    names = {}
    n = GL.glGetProgramiv(p, GL.GL_ACTIVE_UNIFORMS)
    for i in range(n):
        name = GL.glGetActiveUniform(p, i)[0]
        name = name.decode() if isinstance(name, bytes) else name
        names[name] = GL.glGetUniformLocation(p, name)
    return p, names


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------
def _fmt_len(v):
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:.1f}"
    return f"{v:.3g}"


def _fmt_coord(v):
    v = float(v) + 0.0
    if abs(v) < 1e-6:
        v = 0.0
    return f"{v:,.2f}" if abs(v) < 1e5 else f"{v:,.0f}"


def _fmt_num(v, unit):
    if v >= 100:
        return f"{v:,.0f} {unit}"
    return f"{v:,.2f} {unit}"


VIEWS = {   # name: (label, yaw, pitch)
    "front": ("Front", 0.0, 0.0),
    "back": ("Back", 180.0, 0.0),
    "left": ("Left", -90.0, 0.0),
    "right": ("Right", 90.0, 0.0),
    "top": ("Top", 0.0, 90.0),
    "bottom": ("Bottom", 0.0, -90.0),
    "iso": ("Isometric", 45.0, 35.264),
}
DEFAULT_YAW, DEFAULT_PITCH = 30.0, 22.0
FOV = 30.0

COLORS = {
    False: {  # light style
        "bg_top": (0.965, 0.969, 0.976), "bg_bottom": (0.835, 0.851, 0.875),
        "sky": (0.42, 0.44, 0.47), "ground": (0.16, 0.15, 0.15),
        "wire": (0.16, 0.20, 0.28),
        "minor": (0.0, 0.0, 0.0, 0.07), "major": (0.0, 0.0, 0.0, 0.17),
        "shadow": 0.30,
    },
    True: {   # dark style
        "bg_top": (0.235, 0.243, 0.259), "bg_bottom": (0.110, 0.114, 0.125),
        "sky": (0.36, 0.38, 0.42), "ground": (0.10, 0.10, 0.11),
        "wire": (0.12, 0.15, 0.22),
        "minor": (1.0, 1.0, 1.0, 0.05), "major": (1.0, 1.0, 1.0, 0.13),
        "shadow": 0.45,
    },
}
# loading policy
PROGRESSIVE_THRESHOLD = 400_000      # above: show flat first, smooth normals later
COMPACT_MAX_TRIANGLES = 2_000_000    # Quick Look: decimate bigger models ...
COMPACT_TARGET_TRIANGLES = 1_500_000  # ... to about this many triangles
LOD_MIN_TRIANGLES = 4_000_000        # above: build a coarse LOD for smooth orbiting
LOD_TARGET_TRIANGLES = 2_000_000
LOD_SETTLE_SECONDS = 0.25            # full resolution again this long after motion
UPLOAD_SLICE_BYTES = 16 << 20        # GPU upload per frame: ~one chunk (keeps frames < ~25 ms)
CHUNK_VERTICES = 1_500_000           # per VBO (multiple of 3): 18 MB of positions
BASE_COLOR = _srgb_to_linear((0.70, 0.76, 0.84))   # light grey-blue


class _GpuMesh:
    """One drawable, split into chunks of at most CHUNK_VERTICES vertices.
    Each chunk has its own VAO with a positions VBO (float32 xyz, 12 B/vertex)
    and, once phase 2 delivers them, a normals VBO (GL_INT_2_10_10_10_REV,
    4 B/vertex). Chunks are allocated + filled a few per frame so even a
    10M-triangle model never stalls the main loop (one huge glBufferData
    costs >100 ms); the chunks uploaded so far are drawn meanwhile. CPU
    arrays are kept for re-upload after a re-realize."""

    def __init__(self, pos, nrm=None):
        self.pos, self.nrm = pos, nrm
        n = len(pos)
        step = CHUNK_VERTICES
        self.ranges = [(a, min(a + step, n)) for a in range(0, n, step)] or [(0, 0)]
        self.forget_gl()
        self.t_start = None

    def forget_gl(self):
        """Reset ids (after free_gl, or when the context died with them)."""
        k = len(self.ranges)
        self.vao, self.vbo, self.nbo = [None] * k, [None] * k, [None] * k
        self.pos_n = 0             # chunks with positions uploaded
        self.nrm_n = 0             # chunks with normals uploaded

    @property
    def n(self):
        return len(self.pos)

    @property
    def pos_up(self):
        return self.pos_n > 0

    @property
    def nrm_up(self):
        return self.nrm is not None and self.nrm_n == len(self.ranges)

    @property
    def complete(self):
        return self.pos_n == len(self.ranges) and (self.nrm is None or self.nrm_up)

    def set_normals(self, nrm):
        self.nrm = nrm
        self.nrm_n = 0

    def sync(self, budget):
        """Upload whole chunks until `budget` bytes are used (context current)."""
        used = 0
        if self.t_start is None:
            self.t_start = time.perf_counter()
        while self.pos_n < len(self.ranges) and used < budget:
            i = self.pos_n
            a, b = self.ranges[i]
            vao = GL.glGenVertexArrays(1)
            GL.glBindVertexArray(vao)
            vbo = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo)
            data = np.ascontiguousarray(self.pos[a:b])
            GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data, GL.GL_STATIC_DRAW)
            GL.glEnableVertexAttribArray(0)
            GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 12, ctypes.c_void_p(0))
            GL.glDisableVertexAttribArray(1)
            self.vao[i], self.vbo[i] = vao, vbo
            self.pos_n += 1
            used += data.nbytes
            if self.pos_n == len(self.ranges):
                _log(f"uploaded {self.n:,} vertices ({self.pos.nbytes / 1e6:.0f} MB, "
                     f"{len(self.ranges)} chunks) over {time.perf_counter() - self.t_start:.2f}s")
        while (self.nrm is not None and self.pos_n == len(self.ranges)
               and self.nrm_n < len(self.ranges) and used < budget):
            i = self.nrm_n
            a, b = self.ranges[i]
            GL.glBindVertexArray(self.vao[i])
            if self.nbo[i] is None:
                self.nbo[i] = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.nbo[i])
            data = np.ascontiguousarray(self.nrm[a:b])
            GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data, GL.GL_STATIC_DRAW)
            self.nrm_n += 1
            used += data.nbytes
            if self.nrm_n == len(self.ranges):     # switch all chunks over at once
                for j in range(len(self.ranges)):
                    GL.glBindVertexArray(self.vao[j])
                    GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.nbo[j])
                    GL.glEnableVertexAttribArray(1)
                    GL.glVertexAttribPointer(1, 4, GL.GL_INT_2_10_10_10_REV, GL.GL_TRUE, 4,
                                             ctypes.c_void_p(0))
        GL.glBindVertexArray(0)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)
        return used

    def draw(self):
        for i in range(self.pos_n):
            a, b = self.ranges[i]
            GL.glBindVertexArray(self.vao[i])
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, b - a)

    def free_gl(self):
        bufs = [x for x in self.vbo + self.nbo if x is not None]
        if bufs:
            GL.glDeleteBuffers(len(bufs), bufs)
        vaos = [x for x in self.vao if x is not None]
        if vaos:
            GL.glDeleteVertexArrays(len(vaos), vaos)
        self.forget_gl()


class ModelViewer(Viewer):
    def __init__(self, window):
        super().__init__(window)
        _install_icons()
        self._path = None
        self._gfile = None
        self._name = "Model"
        self._mesh = None
        self._display = None          # mesh drawn (== _mesh, or a decimated preview)
        self._gpu = None              # _GpuMesh drawn when the view is still
        self._lod = None              # coarser _GpuMesh drawn while moving (huge models)
        self._gl_trash = []           # _GpuMesh objects to free when the context is current
        self._last_motion = 0.0
        self._settle_source = None
        self._alive = True
        self._load_seconds = None

        # display state
        self.wireframe = False
        self.flat = False
        self.grid = True
        self.autorotate = False
        self.show_stats = False
        self.up_axis = "z"

        # camera
        self.yaw, self.pitch = DEFAULT_YAW, DEFAULT_PITCH
        self.dist = 100.0
        self.target = np.zeros(3)
        self.radius = 1.0
        self.half = np.ones(3)        # world-space half extents of the model
        self._anim = None
        self._velocity = (0.0, 0.0)   # touchpad orbit inertia, deg/s
        self._inertia_tick = None
        self._rotate_tick = None
        self._last_rot_time = None

        # GL state
        self._gl_ready = False
        self._gl_failed = None
        self._progs = {}
        self._empty_vao = None
        self._fbo = None              # (w, h, samples, msaa_fbo, rbs, resolve_fbo, rbs)
        self._fb_size = (1, 1)
        self._needs_fit = False
        self._max_samples = 0
        self._gen = 0                 # load generation (stale worker results are dropped)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               vexpand=True, hexpand=True)
        self.append(self.stack)

        # loading indicator lives on top of the (already realized) GL view, so
        # GL setup + shader compilation happen while the worker parses
        self.spinner_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                                    halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                    can_target=False)
        self.spinner_page.append(Adw.Spinner(width_request=48, height_request=48))
        self.spinner_label = Gtk.Label(label="Opening Model…", css_classes=["title-4", "dim-label"])
        self.spinner_page.append(self.spinner_label)

        self.error_page = Adw.StatusPage(icon_name="dialog-error-symbolic",
                                         title="Can't Open This Model")
        self.stack.add_named(self.error_page, "error")

        self.gl_error_page = Adw.StatusPage(icon_name="video-display-symbolic",
                                            title="3D Rendering Unavailable")
        self.stack.add_named(self.gl_error_page, "gl-error")

        self.glarea = Gtk.GLArea(hexpand=True, vexpand=True, focusable=True)
        self.glarea.set_allowed_apis(Gdk.GLAPI.GL)
        self.glarea.set_required_version(3, 3)
        self.glarea.set_has_depth_buffer(True)
        self.glarea.set_auto_render(True)
        self.glarea.connect("realize", _weak(self._on_realize))
        self.glarea.connect("unrealize", _weak(self._on_unrealize))
        self.glarea.connect("render", _weak(self._on_render))
        self.glarea.connect("resize", _weak(self._on_resize))
        self.stats_label = Gtk.Label(xalign=0, halign=Gtk.Align.START, valign=Gtk.Align.END,
                                     margin_start=12, margin_bottom=12, can_target=False,
                                     css_classes=["osd", "caption", "numeric"], visible=False)
        self.stats_label.set_margin_top(0)
        self.view_overlay = Gtk.Overlay(child=self.glarea)
        self.view_overlay.add_overlay(self.stats_label)
        self.view_overlay.add_overlay(self.spinner_page)
        self.stack.add_named(self.view_overlay, "view")
        self.stack.set_visible_child_name("view")

        self._setup_input()
        self._style = Adw.StyleManager.get_default()
        self._style_handler = None
        self._compact = False
        self.connect("realize", _weak(self._on_self_realize))
        self.connect("unrealize", _weak(self._on_self_unrealize))
        self.connect("destroy", _weak(self._on_destroy))

    # ------------------------------------------------------------------
    # embedding (Quick Look)
    # ------------------------------------------------------------------
    def set_compact(self, compact: bool = True):
        """Compact embedding (Quick Look): no ground grid chrome, smaller
        status pages, no focus grabbing. Orbit / pan / zoom / pinch stay."""
        self._compact = bool(compact)
        self.spinner_label.set_visible(not compact)
        for page in (self.error_page, self.gl_error_page):
            if compact:
                page.add_css_class("compact")
            else:
                page.remove_css_class("compact")
        self.grid = not compact
        self.stats_label.set_visible(self.show_stats and not compact)
        if hasattr(self, "actions"):
            self.actions.lookup_action("grid").set_state(GLib.Variant.new_boolean(self.grid))
        self.glarea.queue_render()

    def release(self):
        """Free everything now (GL objects, mesh memory, timers, signal
        handlers). Safe to call more than once; the viewer is unusable after.
        Hosts that swap viewers in one long-lived process should call this
        (window destruction does it automatically)."""
        if not self._alive and self._mesh is None and self._gpu is None:
            return
        self._alive = False
        self._cancel_anim()
        self._set_autorotate(False)
        self._stop_inertia()
        self._disconnect_style()
        if self._settle_source is not None:
            GLib.source_remove(self._settle_source)
            self._settle_source = None
        if self.glarea.get_realized():
            self._on_unrealize(self.glarea)
        self._gpu = self._lod = None
        self._gl_trash.clear()
        self._display = None
        self._mesh = None

    def _on_self_realize(self, *_):
        if self._style_handler is None:
            ref = weakref.ref(self)

            def on_dark(*_a):
                me = ref()
                if me is not None:
                    me.glarea.queue_render()
            self._style_handler = self._style.connect("notify::dark", on_dark)

    def _on_self_unrealize(self, *_):
        self._disconnect_style()
        self._set_autorotate(False)
        self._stop_inertia()

    def _disconnect_style(self):
        if self._style_handler:
            self._style.disconnect(self._style_handler)
            self._style_handler = None

    # ------------------------------------------------------------------
    # Viewer contract
    # ------------------------------------------------------------------
    def get_title(self):
        return self._name

    def get_subtitle(self):
        if self._mesh is None:
            return ""
        s = self._mesh.stats
        dims = " × ".join(_fmt_len(v) for v in s["size"])
        return f"{s['triangles']:,} triangles · {dims} mm"

    def get_info(self):
        """Sections for the shell's Get Info inspector (Ctrl+I)."""
        sections = []
        if self._gfile is not None:
            try:
                from .viewer_base import file_info_section
                sec = file_info_section(self._gfile)
                if sec:
                    sections.append(sec)
            except ImportError:
                pass                  # the shell prepends the File section itself
        if self._mesh is None:
            return sections
        s = self._mesh.stats
        done = self._mesh.analyzed
        pending = "Computing…" if self._display is self._mesh else "—"
        sx, sy, sz = s["size"]
        mm_in = 25.4
        rows = [("Format", self._mesh.fmt)]
        if self._mesh.header:
            rows.append(("STL header", self._mesh.header))
        rows += [
            ("Triangles", f"{s['triangles']:,}"),
            ("Vertices", f"{s['vertices']:,}" if done else pending),
            ("Dimensions", f"{_fmt_len(sx)} × {_fmt_len(sy)} × {_fmt_len(sz)} mm"),
            ("", f"{sx / mm_in:.2f} × {sy / mm_in:.2f} × {sz / mm_in:.2f} in"),
            ("Bounding box min", ", ".join(_fmt_coord(c) for c in s["bbox_min"])),
            ("Bounding box max", ", ".join(_fmt_coord(c) for c in s["bbox_max"])),
        ]
        if done:
            com = ", ".join(_fmt_coord(c) for c in s["center_of_mass"])
            rows += [
                ("Surface area", _fmt_num(s["area"], "mm²")),
                ("Volume", self._volume_text()),
                ("Watertight", "Yes (closed mesh)" if s["closed"] else self._open_text()),
                ("Centre of mass", com + ("" if s["closed"] else " (approx.)")),
            ]
        else:
            rows += [(k, pending) for k in ("Surface area", "Volume", "Watertight")]
        if self._display is not None and self._display is not self._mesh:
            rows.append(("Displayed", f"preview, {self._display.n_triangles:,} triangles"))
        if self._load_seconds is not None:
            t = f"{self._load_seconds:.2f} s to first frame"
            if getattr(self, "_analyze_seconds", None):
                t += f", {self._analyze_seconds:.2f} s total"
            rows.append(("Load time", t))
        sections.append(("Model", rows))
        return sections

    def _volume_text(self):
        s = self._mesh.stats
        vol = s["volume"]
        txt = _fmt_num(vol, "mm³")
        if vol >= 1000:
            txt += f"  ({vol / 1000:,.2f} cm³)"
        return txt if s["closed"] else "≈ " + txt

    def _open_text(self):
        s = self._mesh.stats
        bits = []
        if s["boundary_edges"]:
            bits.append(f"{s['boundary_edges']:,} open edges")
        if s["nonmanifold_edges"]:
            bits.append(f"{s['nonmanifold_edges']:,} non-manifold edges")
        return "No — " + ", ".join(bits or ["not closed"])

    def load(self, files):
        """Load a model. May be called again on the same viewer to swap the
        model (re-uses the GL context, which is the cheap path for hosts like
        Quick Look: every new GLArea costs GTK/Mesa a few MB that aren't
        returned)."""
        gfile = files[0]
        self._gen += 1
        self._alive = True
        self._gfile = gfile
        self._path = gfile.get_path()
        self._name = gfile.get_basename() or "Model"
        self.spinner_page.set_visible(True)
        if not self._gl_failed:
            self.stack.set_visible_child_name("view")
        self.refresh_title()
        threading.Thread(target=self._load_worker, args=(gfile, self._gen), daemon=True).start()

    def build_header(self, header):
        self._build_actions()
        start = Gtk.Box(spacing=6)
        start.append(Gtk.Button(icon_name="zoom-fit-best-symbolic", action_name="model.reset",
                                tooltip_text="Reset View (R)"))
        views = Gio.Menu()
        sec = Gio.Menu()
        for key, (label, _, _) in VIEWS.items():
            sec.append(label, f"model.view::{key}")
        views.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Z Axis Up", "model.up::z")
        sec.append("Y Axis Up", "model.up::y")
        views.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Auto-Rotate", "model.autorotate")
        sec.append("Show Statistics", "model.stats")
        views.append_section(None, sec)
        start.append(Gtk.MenuButton(icon_name="preview-model-views-symbolic", menu_model=views,
                                    tooltip_text="View"))
        start.append(Gtk.Button(icon_name="camera-photo-symbolic", action_name="model.snapshot",
                                tooltip_text="Export Snapshot…"))
        header.pack_start(start)

        end = Gtk.Box(spacing=6)
        toggles = Gtk.Box(css_classes=["linked"])
        toggles.append(Gtk.ToggleButton(icon_name="preview-model-wireframe-symbolic",
                                        action_name="model.wireframe",
                                        tooltip_text="Wireframe (W)"))
        toggles.append(Gtk.ToggleButton(icon_name="preview-model-shading-symbolic",
                                        action_name="model.flat",
                                        tooltip_text="Flat Shading"))
        toggles.append(Gtk.ToggleButton(icon_name="preview-model-grid-symbolic",
                                        action_name="model.grid",
                                        tooltip_text="Ground Grid"))
        end.append(toggles)
        # packed after the shell's main menu, so it sits just left of it
        header.pack_end(end)

        sc = Gtk.ShortcutController(scope=Gtk.ShortcutScope.GLOBAL)
        for key, action in (("r", "model.reset"), ("w", "model.wireframe")):
            sc.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(key),
                                         action=Gtk.NamedAction.new(action)))
        self.add_controller(sc)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _build_actions(self):
        self.actions = group = Gio.SimpleActionGroup()

        def simple(name, cb, ptype=None):
            a = Gio.SimpleAction.new(name, ptype)
            a.connect("activate", cb)
            group.add_action(a)
            return a

        def toggle(name, attr):
            a = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(getattr(self, attr)))

            def act(action, _param):
                v = not action.get_state().get_boolean()
                action.set_state(GLib.Variant.new_boolean(v))
                setattr(self, attr, v)
                if attr == "autorotate":
                    self._set_autorotate(v)
                if attr == "show_stats":
                    self.stats_label.set_visible(v and not self._compact)
                self.glarea.queue_render()
            a.connect("activate", act)
            group.add_action(a)
            return a

        simple("reset", lambda *_: self.reset_view(animate=True))
        simple("view", lambda _a, p: self.set_view(p.get_string()), GLib.VariantType.new("s"))
        simple("snapshot", lambda *_: self._export_snapshot())
        toggle("wireframe", "wireframe")
        toggle("flat", "flat")
        toggle("grid", "grid")
        toggle("autorotate", "autorotate")
        toggle("stats", "show_stats")
        up = Gio.SimpleAction.new_stateful("up", GLib.VariantType.new("s"), GLib.Variant.new_string(self.up_axis))

        def set_up(action, param):
            action.set_state(param)
            self._set_up_axis(param.get_string())
        up.connect("activate", set_up)
        group.add_action(up)
        host = self.window if isinstance(self.window, Gtk.Widget) else self
        host.insert_action_group("model", group)
        self._set_actions_enabled(False)

    def _set_actions_enabled(self, on):
        if not hasattr(self, "actions"):
            return
        for name in ("reset", "view", "snapshot", "wireframe", "flat", "grid", "autorotate", "stats", "up"):
            self.actions.lookup_action(name).set_enabled(on)

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------
    def _load_worker(self, gfile, gen):
        with _LOAD_LOCK:
            if gen == self._gen and self._alive:
                self._load_locked(gfile, gen)

    def _load_locked(self, gfile, gen):
        """Two phases: (1) parse to a triangle soup + bbox and show it flat
        shaded right away; (2) weld / stats / smooth normals, then swap in the
        normals. Small models do both before the first frame (no flash)."""
        t0 = time.perf_counter()
        try:
            path = gfile.get_path()
            name = gfile.get_basename() or "model.stl"
            if path:
                mesh = mesh_io.read_mesh(path)
            else:
                ok, data, _ = gfile.load_contents(None)
                mesh = mesh_io.read_mesh(name, bytes(data))
                del data
            t1 = time.perf_counter()
            if gen != self._gen:
                return False          # superseded while parsing
            display = mesh
            if self._compact and mesh.n_triangles > COMPACT_MAX_TRIANGLES:
                display = mesh_io.decimate(mesh, COMPACT_TARGET_TRIANGLES)
            quick = display.n_triangles <= PROGRESSIVE_THRESHOLD
            if quick:
                mesh_io.analyze(display, stats=display is mesh)
            pos = mesh_io.positions(display)
            t2 = time.perf_counter()
            _log(f"read {mesh.n_triangles:,} tris in {t1 - t0:.2f}s"
                 + (f", decimated to {display.n_triangles:,}" if display is not mesh else "")
                 + f", first frame data at {t2 - t0:.2f}s")
            GLib.idle_add(self._on_loaded, mesh, display, pos, display.normals, t2 - t0, gen)
            if quick or gen != self._gen:
                return False
            # phase 2 (full stats are skipped for a compact preview of a huge file)
            mesh_io.analyze(display, stats=display is mesh)
            display.face_n = None
            if gen != self._gen:
                return False
            t3 = time.perf_counter()
            _log(f"analysed (weld/stats/normals) in {t3 - t2:.2f}s")
            GLib.idle_add(self._on_analyzed, display.normals, t3 - t0, gen)
            if display.n_triangles > LOD_MIN_TRIANGLES and gen == self._gen:
                lod = mesh_io.decimate(display, LOD_TARGET_TRIANGLES)
                mesh_io.analyze(lod, stats=False)
                if gen == self._gen:
                    GLib.idle_add(self._on_lod, mesh_io.positions(lod), lod.normals, gen)
                _log(f"LOD built in {time.perf_counter() - t3:.2f}s")
        except mesh_io.MeshError as exc:
            GLib.idle_add(self._on_load_error, str(exc), gen)
        except MemoryError:
            GLib.idle_add(self._on_load_error, "Not enough memory to open this model.", gen)
        except GLib.Error as exc:
            GLib.idle_add(self._on_load_error, exc.message, gen)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            GLib.idle_add(self._on_load_error, f"{type(exc).__name__}: {exc}", gen)
        return False

    def _on_load_error(self, message, gen=None):
        if not self._alive or (gen is not None and gen != self._gen):
            return False
        self._mesh = self._display = None
        self._set_gpu(None)
        self._set_lod(None)
        self._set_actions_enabled(False)
        self._fill_info()
        self.error_page.set_description(GLib.markup_escape_text(message))
        self.stack.set_visible_child_name("error")
        self.refresh_title()
        return False

    def _on_loaded(self, mesh, display, pos, normals, seconds, gen=None):
        if not self._alive or (gen is not None and gen != self._gen):
            return False
        self._mesh = mesh
        self._display = display
        self._set_gpu(_GpuMesh(pos, normals))
        self._set_lod(None)
        self._load_seconds = seconds
        self.up_axis = mesh.up
        if hasattr(self, "actions"):
            self.actions.lookup_action("up").set_state(GLib.Variant.new_string(mesh.up))
        self._update_geometry()
        self._fill_info()
        self.refresh_title()
        if self._gl_failed:
            self._show_gl_error(self._gl_failed)
        else:
            self.stack.set_visible_child_name("view")
            self.spinner_page.set_visible(False)
            self._set_actions_enabled(True)
            self.reset_view(animate=False)
            self._needs_fit = True        # refit once the GLArea has its real size
            if not self._compact:
                self.glarea.grab_focus()
        return False

    def _on_analyzed(self, normals, seconds, gen=None):
        if not self._alive or (gen is not None and gen != self._gen):
            return False
        if self._gpu is not None:
            self._gpu.set_normals(normals)
        self.glarea.queue_render()
        self._analyze_seconds = seconds
        self._fill_info()
        self.refresh_title()
        self.glarea.queue_render()
        return False

    # ------------------------------------------------------------------
    # geometry / camera
    # ------------------------------------------------------------------
    def _model_matrix(self):
        return _rx(90.0) if self.up_axis == "y" else np.eye(4)

    def _update_geometry(self):
        if self._mesh is None:
            return
        size = np.asarray(self._mesh.stats["size"], np.float64)
        half = np.abs(self._model_matrix()[:3, :3]) @ (size / 2)
        self.half = half
        self.radius = max(float(np.linalg.norm(half)), 1e-6)
        if not np.isfinite(self.radius) or self.radius <= 0:
            self.radius = 1.0
        mag = 10 ** math.floor(math.log10(self.radius / 6))
        r = (self.radius / 6) / mag
        self.grid_step = mag * (1 if r < 2 else 2 if r < 5 else 5)
        center = (self._mesh.stats["bbox_min"] + self._mesh.stats["bbox_max"]) / 2
        self.grid_offset = (self._model_matrix()[:3, :3] @ center)[:2]

    def _set_up_axis(self, axis):
        if axis == self.up_axis:
            return
        self.up_axis = axis
        self._update_geometry()
        self.reset_view(animate=True)

    def _fit_distance(self, yaw=None, pitch=None):
        """Camera distance at which the model's bounding box exactly fits the
        viewport for the given orientation (plus a small margin)."""
        w, h = max(self.glarea.get_width(), 1), max(self.glarea.get_height(), 1)
        tan_y = math.tan(math.radians(FOV) / 2)
        tan_x = tan_y * w / h
        yaw = self.yaw if yaw is None else yaw
        pitch = self.pitch if pitch is None else pitch
        rot = (_rx(pitch - 90.0) @ _rz(-yaw))[:3, :3]
        signs = np.array([[sx, sy, sz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
        pts = (signs * self.half) @ rot.T            # corners in camera axes
        need = np.maximum(np.abs(pts[:, 0]) / tan_x, np.abs(pts[:, 1]) / tan_y) + pts[:, 2]
        return float(need.max()) * 1.12 + 1e-9

    def reset_view(self, animate=True):
        self._animate_to(DEFAULT_YAW, DEFAULT_PITCH, self._fit_distance(DEFAULT_YAW, DEFAULT_PITCH),
                         np.zeros(3), animate)

    def set_view(self, name):
        if name not in VIEWS:
            return
        _, yaw, pitch = VIEWS[name]
        self._animate_to(yaw, pitch, self._fit_distance(yaw, pitch), np.zeros(3), True)

    def _animate_to(self, yaw, pitch, dist, target, animate):
        if self._anim is not None:
            self._anim.skip()
            self._anim = None
        self._stop_inertia()
        if not animate or not self.glarea.get_mapped():
            self.yaw, self.pitch, self.dist, self.target = yaw, pitch, dist, np.array(target, float)
            self.glarea.queue_render()
            return
        y0, p0, d0, t0 = self.yaw, self.pitch, self.dist, self.target.copy()
        dy = (yaw - y0 + 180.0) % 360.0 - 180.0          # shortest way round
        tgt = np.array(target, float)

        def step(v):
            self.yaw = y0 + dy * v
            self.pitch = p0 + (pitch - p0) * v
            self.dist = d0 * (dist / d0) ** v
            self.target = t0 + (tgt - t0) * v
            self._note_motion()
            self.glarea.queue_render()
        anim = Adw.TimedAnimation.new(self.glarea, 0.0, 1.0, 380, Adw.CallbackAnimationTarget.new(step))
        anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        anim.connect("done", lambda *_: setattr(self, "_anim", None))
        self._anim = anim
        anim.play()

    def _cancel_anim(self):
        if self._anim is not None:
            a, self._anim = self._anim, None
            a.pause()

    def orbit(self, dyaw, dpitch):
        self._needs_fit = False
        self._note_motion()
        self.yaw = (self.yaw + dyaw) % 360.0
        self.pitch = max(-90.0, min(90.0, self.pitch + dpitch))
        self.glarea.queue_render()

    def zoom(self, factor):
        self._needs_fit = False
        self._note_motion()
        r = self.radius
        self.dist = max(r * 0.05, min(r * 60.0, self.dist * factor))
        self.glarea.queue_render()

    def pan(self, dx_px, dy_px):
        self._note_motion()
        h = max(self.glarea.get_height(), 1)
        world_per_px = 2 * self.dist * math.tan(math.radians(FOV) / 2) / h
        rot = (_rx(self.pitch - 90.0) @ _rz(-self.yaw))[:3, :3]
        right, up = rot[0], rot[1]          # camera axes in world space
        self.target = self.target - right * dx_px * world_per_px + up * dy_px * world_per_px
        lim = self.radius * 4
        self.target = np.clip(self.target, -lim, lim)
        self.glarea.queue_render()

    def _view_matrix(self):
        return _translate([0, 0, -self.dist]) @ _rx(self.pitch - 90.0) @ _rz(-self.yaw) @ _translate(-self.target)

    # ------------------------------------------------------------------
    # input
    # ------------------------------------------------------------------
    def _setup_input(self):
        drag = Gtk.GestureDrag(button=0)
        drag.connect("drag-begin", _weak(self._on_drag_begin))
        drag.connect("drag-update", _weak(self._on_drag_update))
        drag.connect("drag-end", _weak(self._on_drag_end))
        self.glarea.add_controller(drag)

        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        click.connect("pressed", _weak(self._on_click))
        self.glarea.add_controller(click)

        flags = Gtk.EventControllerScrollFlags.BOTH_AXES | Gtk.EventControllerScrollFlags.KINETIC
        scroll = Gtk.EventControllerScroll(flags=flags)
        scroll.connect("scroll-begin", _weak(self._on_scroll_begin))
        scroll.connect("scroll", _weak(self._on_scroll))
        scroll.connect("decelerate", _weak(self._on_decelerate))
        self.glarea.add_controller(scroll)

        zoom = Gtk.GestureZoom()
        zoom.connect("begin", _weak(self._on_pinch_begin))
        zoom.connect("scale-changed", _weak(self._on_pinch))
        self.glarea.add_controller(zoom)

    def _on_scroll_begin(self, *_):
        self._stop_inertia()

    def _on_click(self, gesture, n_press, x, y):
        if not self._compact:
            self.glarea.grab_focus()
        if n_press == 2 and self._mesh is not None:
            self.reset_view(animate=True)

    def _on_drag_begin(self, gesture, x, y):
        self._cancel_anim()
        self._stop_inertia()
        button = gesture.get_current_button()
        state = gesture.get_current_event_state()
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        self._drag_mode = "pan" if (button in (2, 3) or shift) else "orbit"
        self._drag_last = (0.0, 0.0)
        self.glarea.set_cursor_from_name("move" if self._drag_mode == "pan" else "grabbing")

    def _on_drag_update(self, gesture, ox, oy):
        lx, ly = self._drag_last
        dx, dy = ox - lx, oy - ly
        self._drag_last = (ox, oy)
        if self._drag_mode == "pan":
            self.pan(dx, dy)
        else:
            self.orbit(-dx * 0.4, dy * 0.4)

    def _on_drag_end(self, gesture, ox, oy):
        self.glarea.set_cursor(None)

    def _on_scroll(self, ctrl, dx, dy):
        if self._mesh is None:
            return False
        self._cancel_anim()
        unit = ctrl.get_unit()
        state = ctrl.get_current_event_state()
        ctrl_held = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if unit == Gdk.ScrollUnit.WHEEL:
            self._scroll_mode = "zoom"
            self.zoom(1.15 ** dy)
        elif ctrl_held:
            self._scroll_mode = "zoom"
            self.zoom(math.exp(dy * 0.01))
        else:
            # touchpad two-finger scroll orbits; the deltas already follow the
            # user's natural-scrolling setting, so the model tracks the fingers
            self._scroll_mode = "orbit"
            self.orbit(dx * 0.35, -dy * 0.35)
        return True

    def _on_decelerate(self, ctrl, vx, vy):
        if getattr(self, "_scroll_mode", None) != "orbit" or ctrl.get_unit() == Gdk.ScrollUnit.WHEEL:
            return
        vel = (vx * 0.35, -vy * 0.35)
        if math.hypot(*vel) < 20:
            return
        self._velocity = vel
        self._inertia_last = None
        if self._inertia_tick is None:
            self._inertia_tick = self.glarea.add_tick_callback(self._inertia_step)

    def _inertia_step(self, widget, clock):
        now = clock.get_frame_time() / 1e6
        last = self._inertia_last
        self._inertia_last = now
        if last is None:
            return GLib.SOURCE_CONTINUE
        dt = min(now - last, 0.05)
        vx, vy = self._velocity
        self.orbit(vx * dt, vy * dt)
        k = math.exp(-dt * 4.5)
        self._velocity = (vx * k, vy * k)
        if math.hypot(vx, vy) < 3:
            self._inertia_tick = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _stop_inertia(self):
        if self._inertia_tick is not None:
            self.glarea.remove_tick_callback(self._inertia_tick)
            self._inertia_tick = None

    def _on_pinch_begin(self, gesture, seq):
        self._cancel_anim()
        self._stop_inertia()
        self._pinch_dist = self.dist

    def _on_pinch(self, gesture, scale):
        if scale > 0:
            r = self.radius
            self.dist = max(r * 0.05, min(r * 60.0, self._pinch_dist / scale))
            self._note_motion()
            self.glarea.queue_render()

    def _set_autorotate(self, on):
        if on and self._rotate_tick is None:
            self._last_rot_time = None
            self._rotate_tick = self.glarea.add_tick_callback(self._rotate_step)
        elif not on and self._rotate_tick is not None:
            self.glarea.remove_tick_callback(self._rotate_tick)
            self._rotate_tick = None

    def _rotate_step(self, widget, clock):
        now = clock.get_frame_time() / 1e6
        if self._last_rot_time is not None:
            self.orbit(18.0 * min(now - self._last_rot_time, 0.1), 0.0)
        self._last_rot_time = now
        return GLib.SOURCE_CONTINUE

    # ------------------------------------------------------------------
    # info popover
    # ------------------------------------------------------------------
    def _fill_info(self):
        """Refresh the on-canvas statistics overlay (full details live in the
        shell's Inspector via get_info())."""
        if self._mesh is None:
            self.stats_label.set_label("")
            return
        s = self._mesh.stats
        dims = " × ".join(_fmt_len(v) for v in s["size"])
        if not self._mesh.analyzed:
            self.stats_label.set_label(f"{s['triangles']:,} triangles\n{dims} mm\nComputing…")
            return
        lines = [f"{s['triangles']:,} triangles · {s['vertices']:,} vertices",
                 f"{dims} mm",
                 f"Area {_fmt_num(s['area'], 'mm²')}",
                 f"Volume {self._volume_text()}"]
        if not s["closed"]:
            lines.append("Not watertight")
        self.stats_label.set_label("\n".join(lines))

    # ------------------------------------------------------------------
    # GL lifecycle
    # ------------------------------------------------------------------
    def _show_gl_error(self, message):
        self._gl_failed = message or "Unknown error"
        desc = ("View couldn't start OpenGL 3.3, which is needed to display 3D models.\n\n"
                f"<small>{GLib.markup_escape_text(self._gl_failed)}</small>")
        if self._mesh is not None and not self._compact:
            desc += "\n\nThe model's details are still available in the Inspector (Ctrl+I)."
        self.gl_error_page.set_description(desc)
        if self._mesh is not None or self.stack.get_visible_child_name() == "view":
            self.stack.set_visible_child_name("gl-error")
        if hasattr(self, "actions"):
            self._set_actions_enabled(False)
        return False

    def _on_realize(self, area):
        t_real = time.perf_counter()
        try:
            self._realize_inner(area)
        finally:
            _log(f"GL realize took {(time.perf_counter() - t_real) * 1000:.0f} ms")

    def _realize_inner(self, area):
        if _GL_IMPORT_ERROR is not None:
            GLib.idle_add(self._show_gl_error, f"PyOpenGL unavailable: {_GL_IMPORT_ERROR}")
            return
        area.make_current()
        err = area.get_error()
        if err is not None:
            GLib.idle_add(self._show_gl_error, err.message)
            return
        try:
            self._progs["mesh"] = _compile(MESH_VS, MESH_FS)
            self._progs["bg"] = _compile(BG_VS, BG_FS)
            self._progs["grid"] = _compile(GRID_VS, GRID_FS)
            self._empty_vao = GL.glGenVertexArrays(1)
            self._max_samples = int(GL.glGetIntegerv(GL.GL_MAX_SAMPLES))
            _log("GL", GL.glGetString(GL.GL_VERSION), GL.glGetString(GL.GL_RENDERER),
                 "max samples", self._max_samples)
            self._gl_ready = True
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            GLib.idle_add(self._show_gl_error, str(exc))

    def _on_unrealize(self, area):
        if not self._gl_ready:
            return
        area.make_current()
        if area.get_error() is not None:
            return
        try:
            for prog, _ in self._progs.values():
                GL.glDeleteProgram(prog)
            self._progs.clear()
            for g in self._all_gpu():
                g.free_gl()
            self._gl_trash.clear()
            if self._empty_vao is not None:
                GL.glDeleteVertexArrays(1, [self._empty_vao])
            self._delete_fbo()
        except Exception as exc:  # noqa: BLE001
            _log("unrealize cleanup failed:", exc)
        self._progs.clear()
        for g in self._all_gpu():
            g.forget_gl()
        self._empty_vao = None
        self._fbo = None
        self._gl_ready = False

    # ---- drawables ----------------------------------------------------
    @property
    def _uploaded(self):
        return self._gpu is not None and self._gpu.pos_up

    @property
    def _nrm_uploaded(self):
        return self._gpu is not None and self._gpu.nrm_up

    @property
    def _nverts(self):
        return self._gpu.n if self._gpu is not None else 0

    def _set_gpu(self, g):
        if self._gpu is not None:
            self._gl_trash.append(self._gpu)
        self._gpu = g
        self.glarea.queue_render()

    def _set_lod(self, g):
        if self._lod is not None:
            self._gl_trash.append(self._lod)
        self._lod = g

    def _all_gpu(self):
        return [g for g in (self._gpu, self._lod, *self._gl_trash) if g is not None]

    def _sync_buffers(self, unlimited=False):
        """Free stale buffers and upload at most UPLOAD_SLICE_BYTES this frame
        (main drawable first, then the LOD); re-queue a frame if more is left."""
        for g in self._gl_trash:
            g.free_gl()
        self._gl_trash.clear()
        budget = 1 << 62 if unlimited else UPLOAD_SLICE_BYTES
        for g in (self._gpu, self._lod):
            if g is not None and not g.complete and budget > 0:
                budget -= g.sync(budget)
        if any(g is not None and not g.complete for g in (self._gpu, self._lod)):
            GLib.idle_add(self._queue_render_idle)

    def _queue_render_idle(self):
        if self._alive:
            self.glarea.queue_render()
        return False

    def _on_lod(self, pos, nrm, gen):
        if self._alive and gen == self._gen:
            self._set_lod(_GpuMesh(pos, nrm))
            self.glarea.queue_render()           # starts the (sliced) upload
            _log(f"motion LOD ready: {len(pos) // 3:,} triangles")
        return False

    def _note_motion(self):
        """Camera moved: draw the coarse LOD (if any) until things settle."""
        self._last_motion = time.monotonic()
        if self._lod is not None and self._settle_source is None:
            self._settle_source = GLib.timeout_add(120, self._settle_check)

    def _settle_check(self):
        if time.monotonic() - self._last_motion < LOD_SETTLE_SECONDS or self._camera_busy():
            return GLib.SOURCE_CONTINUE
        self._settle_source = None
        self.glarea.queue_render()            # full-resolution frame
        return GLib.SOURCE_REMOVE

    def _camera_busy(self):
        return (self._anim is not None or self._rotate_tick is not None
                or self._inertia_tick is not None)

    def _pick_drawable(self, final=False):
        g = self._gpu
        if (not final and self._lod is not None and self._lod.complete
                and (time.monotonic() - self._last_motion < LOD_SETTLE_SECONDS or self._camera_busy())):
            return self._lod
        return g

    def _on_resize(self, area, w, h):
        self._fb_size = (max(w, 1), max(h, 1))

    def _delete_fbo(self):
        if self._fbo is None:
            return
        _, _, _, fbos, rbs = self._fbo
        GL.glDeleteFramebuffers(len(fbos), fbos)
        GL.glDeleteRenderbuffers(len(rbs), rbs)
        self._fbo = None

    def _make_fbo(self, w, h, samples):
        """Returns (draw_fbo, resolve_fbo, fbos, rbs); draw_fbo is MSAA when samples>1."""
        fbos, rbs = [], []

        def target(n_samples, depth):
            fbo = GL.glGenFramebuffers(1)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
            color = GL.glGenRenderbuffers(1)
            GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, color)
            GL.glRenderbufferStorageMultisample(GL.GL_RENDERBUFFER, n_samples, GL.GL_RGBA8, w, h)
            GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_RENDERBUFFER, color)
            rbs.append(color)
            if depth:
                d = GL.glGenRenderbuffers(1)
                GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, d)
                GL.glRenderbufferStorageMultisample(GL.GL_RENDERBUFFER, n_samples, GL.GL_DEPTH_COMPONENT24, w, h)
                GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT, GL.GL_RENDERBUFFER, d)
                rbs.append(d)
            fbos.append(fbo)
            ok = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER) == GL.GL_FRAMEBUFFER_COMPLETE
            return fbo if ok else None

        if samples > 1:
            draw = target(samples, True)
            resolve = target(0, False)
        else:
            draw = resolve = target(0, True)
        GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, 0)
        if draw is None or resolve is None:
            GL.glDeleteFramebuffers(len(fbos), fbos)
            GL.glDeleteRenderbuffers(len(rbs), rbs)
            return None
        return draw, resolve, fbos, rbs

    def _ensure_fbo(self, w, h):
        samples = min(4, getattr(self, "_max_samples", 0))
        if self._fbo is not None and self._fbo[0] == w and self._fbo[1] == h:
            return self._fbo
        self._delete_fbo()
        made = self._make_fbo(w, h, samples) or (self._make_fbo(w, h, 0) if samples > 1 else None)
        if made is None:
            return None
        draw, resolve, fbos, rbs = made
        self._fbo = (w, h, (draw, resolve), fbos, rbs)
        return self._fbo

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def _on_render(self, area, context):
        if not self._gl_ready or self._gl_failed:
            return True
        if DEBUG:
            t = time.perf_counter()
            try:
                return self._render_inner()
            finally:
                d = (time.perf_counter() - t) * 1000
                if d > 20:
                    _log(f"slow frame: {d:.0f} ms")
        return self._render_inner()

    def _render_inner(self):
        try:
            if self._needs_fit and self.glarea.get_width() > 1:
                self._needs_fit = False
                self.dist = self._fit_distance()
            self._sync_buffers()
            gtk_fbo = int(GL.glGetIntegerv(GL.GL_DRAW_FRAMEBUFFER_BINDING))
            w, h = self._fb_size
            fb = self._ensure_fbo(w, h)
            if fb is None:          # no offscreen buffers: draw straight into GTK's
                self._draw_scene(gtk_fbo, w, h)
                return True
            draw, resolve = fb[2]
            self._draw_scene(draw, w, h)
            if draw != resolve:
                self._blit(draw, resolve, w, h)
            self._blit(resolve, gtk_fbo, w, h)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, gtk_fbo)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._gl_failed = str(exc)
            GLib.idle_add(self._show_gl_error, f"Rendering failed: {exc}")
        return True

    @staticmethod
    def _blit(src, dst, w, h):
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, src)
        GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, dst)
        GL.glBlitFramebuffer(0, 0, w, h, 0, 0, w, h, GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)

    def _draw_scene(self, fbo, w, h):
        dark = self._style.get_dark()
        pal = COLORS[dark]
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
        GL.glViewport(0, 0, w, h)
        GL.glDisable(GL.GL_BLEND)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glClearColor(*pal["bg_bottom"], 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)

        # background gradient
        GL.glDisable(GL.GL_DEPTH_TEST)
        prog, u = self._progs["bg"]
        GL.glUseProgram(prog)
        GL.glUniform3f(u["u_top"], *pal["bg_top"])
        GL.glUniform3f(u["u_bottom"], *pal["bg_bottom"])
        GL.glBindVertexArray(self._empty_vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)

        g = self._pick_drawable(final=getattr(self, "_final_frame", False))
        if g is None or not g.pos_up:
            GL.glBindVertexArray(0)
            return

        logical_w = max(self.glarea.get_width(), 1)
        px = w / logical_w                                     # device px per logical px
        r = self.radius
        near = max(self.dist - 8 * r, self.dist * 0.01, r * 1e-3)
        far = self.dist + 8 * r
        proj = _perspective(FOV, w / h, near, far)
        view = self._view_matrix()
        model = self._model_matrix()
        mv = view @ model
        nmat = np.linalg.inv(mv[:3, :3]).T
        up_view = view[:3, :3] @ np.array([0.0, 0.0, 1.0])

        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LESS)
        prog, u = self._progs["mesh"]
        GL.glUseProgram(prog)
        GL.glUniformMatrix4fv(u["u_mv"], 1, GL.GL_TRUE, mv.astype(np.float32))
        GL.glUniformMatrix4fv(u["u_proj"], 1, GL.GL_TRUE, proj)
        GL.glUniformMatrix3fv(u["u_nmat"], 1, GL.GL_TRUE, nmat.astype(np.float32))
        GL.glUniform1i(u["u_flat"], int(self.flat or not g.nrm_up))
        GL.glUniform1i(u["u_wire"], int(self.wireframe))
        GL.glUniform1f(u["u_px"], px)
        GL.glUniform3f(u["u_base"], *BASE_COLOR)
        GL.glUniform3f(u["u_sky"], *_srgb_to_linear(pal["sky"]))
        GL.glUniform3f(u["u_ground"], *_srgb_to_linear(pal["ground"]))
        GL.glUniform3f(u["u_up"], *up_view.astype(np.float32))
        GL.glUniform3f(u["u_wire_col"], *pal["wire"])
        g.draw()

        if self.grid:
            prog, u = self._progs["grid"]
            GL.glUseProgram(prog)
            GL.glEnable(GL.GL_BLEND)
            GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ZERO, GL.GL_ONE)
            GL.glDepthMask(GL.GL_FALSE)
            mvp = (proj @ view).astype(np.float32)
            extent = max(r * 2.4, self.grid_step * 10)
            GL.glUniformMatrix4fv(u["u_mvp"], 1, GL.GL_TRUE, mvp)
            GL.glUniform1f(u["u_extent"], extent)
            GL.glUniform1f(u["u_z"], -self.half[2] - r * 1e-3)
            GL.glUniform1f(u["u_step"], self.grid_step)
            GL.glUniform2f(u["u_offset"], *self.grid_offset)
            GL.glUniform1f(u["u_px"], px)
            GL.glUniform4f(u["u_minor"], *pal["minor"])
            GL.glUniform4f(u["u_major"], *pal["major"])
            GL.glUniform2f(u["u_shadow"], self.half[0] * 1.05 + r * 0.08, self.half[1] * 1.05 + r * 0.08)
            GL.glUniform1f(u["u_shadow_a"], pal["shadow"])
            GL.glUniform1f(u["u_below"], 1.0 if self.pitch < -0.5 else 0.0)
            GL.glBindVertexArray(self._empty_vao)
            GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
            GL.glDepthMask(GL.GL_TRUE)
            GL.glDisable(GL.GL_BLEND)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)

    # ------------------------------------------------------------------
    # snapshot
    # ------------------------------------------------------------------
    def render_image(self, w=None, h=None):
        """Render the current view offscreen and return a PIL RGB image."""
        from PIL import Image
        if not self._gl_ready:
            raise RuntimeError("3D view is not ready")
        self.glarea.make_current()
        if w is None:
            w, h = self._fb_size
        self._sync_buffers(unlimited=True)
        samples = min(4, getattr(self, "_max_samples", 0))
        made = self._make_fbo(w, h, samples) or self._make_fbo(w, h, 0)
        if made is None:
            raise RuntimeError("could not create an offscreen framebuffer")
        draw, resolve, fbos, rbs = made
        try:
            self._final_frame = True
            self._draw_scene(draw, w, h)
            self._final_frame = False
            if draw != resolve:
                self._blit(draw, resolve, w, h)
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, resolve)
            GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
            data = GL.glReadPixels(0, 0, w, h, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
        finally:
            GL.glDeleteFramebuffers(len(fbos), fbos)
            GL.glDeleteRenderbuffers(len(rbs), rbs)
        arr = np.frombuffer(data, np.uint8).reshape(h, w, 3)[::-1]
        return Image.fromarray(np.ascontiguousarray(arr), "RGB")

    def _export_snapshot(self):
        if not self._gl_ready or self._mesh is None:
            return
        try:
            img = self.render_image()
        except Exception as exc:  # noqa: BLE001
            self.toast(f"Couldn't render snapshot: {exc}")
            return
        finally:
            self.glarea.queue_render()
        stem = os.path.splitext(self._name)[0] or "model"
        dialog = Gtk.FileDialog(title="Export Snapshot", initial_name=f"{stem}.png")
        if self._path:
            dialog.set_initial_folder(Gio.File.new_for_path(os.path.dirname(self._path)))
        f = Gtk.FileFilter(name="PNG Image")
        f.add_mime_type("image/png")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dialog.set_filters(filters)

        def done(dlg, res):
            try:
                gfile = dlg.save_finish(res)
            except GLib.Error:
                return              # cancelled
            path = gfile.get_path()
            if not path:
                self.toast("Can only save snapshots to local files")
                return
            if not path.lower().endswith(".png"):
                path += ".png"
            def save():                # PNG encoding takes ~0.3 s: off the main loop
                try:
                    img.save(path, "PNG")
                except OSError as exc:
                    GLib.idle_add(self.toast, f"Couldn't save snapshot: {exc.strerror or exc}")
                    return
                uri = Gio.File.new_for_path(path).get_uri()
                GLib.idle_add(self.toast, f"Saved {os.path.basename(path)}", "Open",
                              lambda: Gio.AppInfo.launch_default_for_uri(uri, None))
            threading.Thread(target=save, daemon=True).start()
        parent = self.window if isinstance(self.window, Gtk.Window) else self.get_root()
        dialog.save(parent if isinstance(parent, Gtk.Window) else None, None, done)

    # ------------------------------------------------------------------
    def _on_destroy(self, *_):
        self.release()
