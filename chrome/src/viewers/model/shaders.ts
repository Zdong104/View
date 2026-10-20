// GLSL for the model view, ported from preview/stl_view.py. three.js turns these
// into GLSL ES 3.00 (WebGL2): `attribute`/`varying`/`gl_FragColor` are rewritten,
// gl_VertexID and the derivative functions are built in.

/** Mesh: flat shading from screen-space derivatives, or smooth with the normal attribute. */
export const MESH_VS = /* glsl */ `
attribute vec4 a_nrm;
varying vec3 v_pos;
varying vec3 v_nrm;
varying vec3 v_bary;
void main() {
  vec4 p = modelViewMatrix * vec4(position, 1.0);
  v_pos = p.xyz;
  v_nrm = normalMatrix * a_nrm.xyz;
  int c = gl_VertexID % 3;
  v_bary = vec3(c == 0, c == 1, c == 2);
  gl_Position = projectionMatrix * p;
}`;

export const MESH_FS = /* glsl */ `
varying vec3 v_pos;
varying vec3 v_nrm;
varying vec3 v_bary;
uniform int u_flat;        // 1 = flat shading (also used until normals arrive)
uniform int u_wire;
uniform float u_px;
uniform vec3 u_base;       // linear
uniform vec3 u_sky;        // linear ambient from above
uniform vec3 u_ground;     // linear ambient from below
uniform vec3 u_up;         // world up in view space
uniform vec3 u_wire_col;   // sRGB
void main() {
  vec3 fn = normalize(cross(dFdx(v_pos), dFdy(v_pos)));   // faces the viewer
  vec3 n = fn;
  if (u_flat == 0 && dot(v_nrm, v_nrm) > 0.01) {
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
  gl_FragColor = vec4(col, 1.0);
}`;

/** Full-screen gradient background with a faint vignette and dithering. */
export const BG_VS = /* glsl */ `
varying vec2 v_uv;
void main() {
  v_uv = position.xy * 0.5 + 0.5;
  gl_Position = vec4(position.xy, 0.0, 1.0);
}`;

export const BG_FS = /* glsl */ `
varying vec2 v_uv;
uniform vec3 u_top;
uniform vec3 u_bottom;
float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
void main() {
  float t = smoothstep(0.0, 1.0, v_uv.y);
  vec3 c = mix(u_bottom, u_top, t);
  vec2 q = v_uv - vec2(0.5, 0.55);
  c *= 1.0 - 0.10 * dot(q, q);
  c += (hash(gl_FragCoord.xy) - 0.5) / 255.0;
  gl_FragColor = vec4(c, 1.0);
}`;

/** Ground grid with a soft footprint shadow under the model. */
export const GRID_VS = /* glsl */ `
uniform float u_extent;
uniform float u_z;
varying vec2 v_xy;
void main() {
  v_xy = position.xy * u_extent;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(v_xy, u_z, 1.0);
}`;

export const GRID_FS = /* glsl */ `
varying vec2 v_xy;
uniform float u_step;
uniform vec2 u_offset;
uniform float u_extent;
uniform float u_px;
uniform vec4 u_minor;      // sRGB + alpha
uniform vec4 u_major;
uniform vec2 u_shadow;     // footprint half size
uniform float u_shadow_a;
uniform float u_below;     // 1 when looking from underneath
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
  gl_FragColor = vec4(rgb, a * mix(1.0, 0.35, u_below));
}`;
