// Orbit camera maths (src/viewers/model/camera.ts), ported from preview/stl_view.py.
import assert from "node:assert/strict";
import { test } from "node:test";
import { OrbitCamera, cameraAxes, fitDistance, gridStep } from "../../src/viewers/model/camera.ts";
import { fmtCoord, fmtLen, fmtNum } from "../../src/viewers/model/format.ts";

const close = (a: number[], b: number[]) => a.forEach((v, i) => assert.ok(Math.abs(v - b[i]!) < 1e-9, `${a} vs ${b}`));

test("camera axes: front looks along +Y, top looks down, bases are orthonormal", () => {
  const front = cameraAxes(0, 0);
  close(front.right, [1, 0, 0]);
  close(front.up, [0, 0, 1]);
  close(front.back, [0, -1, 0]);
  const top = cameraAxes(0, 90);
  close(top.back, [0, 0, 1]);
  close(top.up, [0, 1, 0]);
  const a = cameraAxes(37, 22);
  const dot = (u: number[], v: number[]) => u[0]! * v[0]! + u[1]! * v[1]! + u[2]! * v[2]!;
  assert.ok(Math.abs(dot(a.right, a.up)) < 1e-12 && Math.abs(dot(a.up, a.back)) < 1e-12 && Math.abs(dot(a.right, a.back)) < 1e-12);
});

test("fit distance grows with the model and shrinks for wider viewports", () => {
  const d1 = fitDistance([5, 5, 5], 800, 600, 30, 22);
  assert.ok(fitDistance([10, 10, 10], 800, 600, 30, 22) > d1 * 1.9);
  assert.ok(fitDistance([20, 2, 2], 400, 800, 0, 0) > fitDistance([20, 2, 2], 1600, 800, 0, 0));
  // front view of a 2x2x2 cube, square viewport: the whole box fits within the field of view
  const d = fitDistance([1, 1, 1], 500, 500, 0, 0);
  assert.ok(d > 1 / Math.tan(Math.PI / 12) && d < 3 / Math.tan(Math.PI / 12), String(d));
});

test("orbit clamps pitch and wraps yaw; zoom is bounded; pan stays near the model", () => {
  const c = new OrbitCamera();
  c.setModel([10, 20, 30], "z");
  c.orbit(-45, 100);
  assert.equal(c.pitch, 90);
  assert.equal(c.yaw, 345);
  c.dist = 100;
  for (let i = 0; i < 200; i++) c.zoom(0.5);
  assert.ok(c.dist >= c.radius * 0.05 - 1e-9);
  for (let i = 0; i < 200; i++) c.pan(1e5, 0, 600);
  assert.ok(Math.hypot(...c.target) <= c.radius * 4 * Math.sqrt(3) + 1e-6);
  c.setModel([10, 20, 30], "y"); // Y-up files swap the Y and Z extents
  assert.deepEqual(c.half, [5, 15, 10]);
});

test("grid step and number formatting follow the desktop", () => {
  assert.equal(gridStep(60), 10);
  assert.equal(gridStep(12), 2);
  assert.equal(gridStep(30), 5);
  assert.equal(fmtLen(0.123456), "0.123");
  assert.equal(fmtLen(12.34), "12.3");
  assert.equal(fmtLen(12345.6), "12,346");
  assert.equal(fmtCoord(-0.0000001), "0.00");
  assert.equal(fmtCoord(1234.567), "1,234.57");
  assert.equal(fmtNum(2366.0254, "mm²"), "2,366 mm²");
  assert.equal(fmtNum(2.366, "mm²"), "2.37 mm²");
});
