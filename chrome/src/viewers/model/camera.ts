// Orbit camera maths, a port of the camera helpers in preview/stl_view.py.
// World is Z-up. View matrix: T(0,0,-dist) · Rx(pitch-90) · Rz(-yaw) · T(-target),
// so yaw 0 / pitch 0 looks along +Y ("front"), pitch 90 looks straight down.
export type Vec3 = [number, number, number];

export const FOV = 30;
export const DEFAULT_YAW = 30;
export const DEFAULT_PITCH = 22;

export const VIEWS = {
  front: ["Front", 0, 0],
  back: ["Back", 180, 0],
  left: ["Left", -90, 0],
  right: ["Right", 90, 0],
  top: ["Top", 0, 90],
  bottom: ["Bottom", 0, -90],
  iso: ["Isometric", 45, 35.264],
} as const satisfies Record<string, readonly [string, number, number]>;
export type ViewName = keyof typeof VIEWS;

const rad = (d: number) => (d * Math.PI) / 180;

/** Camera axes in world coordinates: the rows of Rx(pitch-90)·Rz(-yaw). `back` points at the viewer. */
export function cameraAxes(yaw: number, pitch: number): { right: Vec3; up: Vec3; back: Vec3 } {
  const cy = Math.cos(rad(yaw)), sy = Math.sin(rad(yaw));
  const ca = Math.cos(rad(pitch - 90)), sa = Math.sin(rad(pitch - 90));
  return {
    right: [cy, sy, 0],
    up: [-ca * sy, ca * cy, -sa],
    back: [-sa * sy, sa * cy, ca],
  };
}

/** Camera distance at which a box with these half extents (world axes) fits the viewport. */
export function fitDistance(half: Vec3, width: number, height: number, yaw: number, pitch: number): number {
  const tanY = Math.tan(rad(FOV) / 2);
  const tanX = (tanY * Math.max(width, 1)) / Math.max(height, 1);
  const { right, up, back } = cameraAxes(yaw, pitch);
  let best = 0;
  for (const sx of [-1, 1]) for (const sy of [-1, 1]) for (const sz of [-1, 1]) {
    const p: Vec3 = [sx * half[0], sy * half[1], sz * half[2]];
    const dot = (a: Vec3) => a[0] * p[0] + a[1] * p[1] + a[2] * p[2];
    const need = Math.max(Math.abs(dot(right)) / tanX, Math.abs(dot(up)) / tanY) + dot(back);
    if (need > best) best = need;
  }
  return best * 1.12 + 1e-9;
}

/** Ground grid spacing: 1, 2 or 5 times a power of ten, about radius/6. */
export function gridStep(radius: number): number {
  const mag = 10 ** Math.floor(Math.log10(radius / 6));
  const r = radius / 6 / mag;
  return mag * (r < 2 ? 1 : r < 5 ? 2 : 5);
}

export class OrbitCamera {
  yaw = DEFAULT_YAW;
  pitch = DEFAULT_PITCH;
  dist = 100;
  target: Vec3 = [0, 0, 0];
  radius = 1;
  /** World-space half extents of the model. */
  half: Vec3 = [1, 1, 1];

  setModel(size: readonly number[], up: "z" | "y"): void {
    const [sx, sy, sz] = [size[0]! / 2, size[1]! / 2, size[2]! / 2];
    // Y-up files are rotated 90° about X: (x, y, z) -> (x, -z, y)
    this.half = up === "y" ? [sx, sz, sy] : [sx, sy, sz];
    const r = Math.hypot(...this.half);
    this.radius = Number.isFinite(r) && r > 0 ? Math.max(r, 1e-6) : 1;
  }

  orbit(dyaw: number, dpitch: number): void {
    this.yaw = (((this.yaw + dyaw) % 360) + 360) % 360;
    this.pitch = Math.max(-90, Math.min(90, this.pitch + dpitch));
  }

  zoom(factor: number): void {
    const r = this.radius;
    this.dist = Math.max(r * 0.05, Math.min(r * 60, this.dist * factor));
  }

  /** Pan by a pixel delta of a viewport `heightPx` tall. */
  pan(dx: number, dy: number, heightPx: number): void {
    const wpp = (2 * this.dist * Math.tan(rad(FOV) / 2)) / Math.max(heightPx, 1);
    const { right, up } = cameraAxes(this.yaw, this.pitch);
    const lim = this.radius * 4;
    for (let i = 0; i < 3; i++) {
      this.target[i] = Math.max(-lim, Math.min(lim, this.target[i]! - right[i]! * dx * wpp + up[i]! * dy * wpp));
    }
  }

  fit(width: number, height: number, yaw = this.yaw, pitch = this.pitch): number {
    return fitDistance(this.half, width, height, yaw, pitch);
  }
}
