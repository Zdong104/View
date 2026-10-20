// three.js scene for the model view: gradient background, the mesh (one non-indexed
// position buffer, flat or smooth shaded), ground grid with footprint shadow.
// The look is a port of preview/stl_view.py (same lights, colours and shaders).
import {
  BufferAttribute, BufferGeometry, DoubleSide, Int8BufferAttribute, Matrix4, Mesh, PerspectiveCamera,
  PlaneGeometry, Scene, ShaderMaterial, Sphere, Vector2, Vector3, Vector4, WebGLRenderer,
} from "three";
import { FOV, cameraAxes, gridStep, type OrbitCamera, type Vec3 } from "./camera.ts";
import { srgbToLinear } from "./format.ts";
import { BG_FS, BG_VS, GRID_FS, GRID_VS, MESH_FS, MESH_VS } from "./shaders.ts";

type Rgb = [number, number, number];
interface Palette {
  bgTop: Rgb; bgBottom: Rgb; sky: Rgb; ground: Rgb; wire: Rgb;
  minor: [number, number, number, number]; major: [number, number, number, number]; shadow: number;
}
const PALETTE: Record<"light" | "dark", Palette> = {
  light: {
    bgTop: [0.965, 0.969, 0.976], bgBottom: [0.835, 0.851, 0.875],
    sky: [0.42, 0.44, 0.47], ground: [0.16, 0.15, 0.15], wire: [0.16, 0.20, 0.28],
    minor: [0, 0, 0, 0.07], major: [0, 0, 0, 0.17], shadow: 0.30,
  },
  dark: {
    bgTop: [0.235, 0.243, 0.259], bgBottom: [0.110, 0.114, 0.125],
    sky: [0.36, 0.38, 0.42], ground: [0.10, 0.10, 0.11], wire: [0.12, 0.15, 0.22],
    minor: [1, 1, 1, 0.05], major: [1, 1, 1, 0.13], shadow: 0.45,
  },
};
const BASE_COLOR = srgbToLinear([0.70, 0.76, 0.84]); // light grey-blue

export interface DisplayState {
  wireframe: boolean;
  flat: boolean;
  grid: boolean;
  dark: boolean;
  up: "z" | "y";
}

export class ModelScene {
  readonly renderer: WebGLRenderer;
  readonly canvas: HTMLCanvasElement;
  private scene = new Scene();
  private camera = new PerspectiveCamera(FOV, 1, 0.1, 1000);
  private geometry = new BufferGeometry();
  private position: BufferAttribute | null = null;
  private meshMat: ShaderMaterial;
  private gridMat: ShaderMaterial;
  private bgMat: ShaderMaterial;
  private mesh: Mesh;
  private grid: Mesh;
  private hasNormals = false;
  private origin: Vec3 = [0, 0, 0];

  constructor(opts: { antialias: boolean }) {
    this.renderer = new WebGLRenderer({ antialias: opts.antialias, alpha: false, powerPreference: "high-performance" });
    this.canvas = this.renderer.domElement;
    this.canvas.className = "model-canvas";
    this.renderer.autoClear = true;

    const v3 = (c: Rgb) => new Vector3(...c);
    this.meshMat = new ShaderMaterial({
      vertexShader: MESH_VS, fragmentShader: MESH_FS,
      uniforms: {
        u_flat: { value: 1 }, u_wire: { value: 0 }, u_px: { value: 1 },
        u_base: { value: v3(BASE_COLOR) }, u_sky: { value: new Vector3() }, u_ground: { value: new Vector3() },
        u_up: { value: new Vector3(0, 0, 1) }, u_wire_col: { value: new Vector3() },
      },
    });
    this.mesh = new Mesh(this.geometry, this.meshMat);
    this.mesh.frustumCulled = false;

    this.gridMat = new ShaderMaterial({
      vertexShader: GRID_VS, fragmentShader: GRID_FS,
      transparent: true, depthWrite: false, side: DoubleSide,
      uniforms: {
        u_extent: { value: 1 }, u_z: { value: 0 }, u_step: { value: 1 }, u_offset: { value: new Vector2() },
        u_px: { value: 1 }, u_minor: { value: new Vector4() }, u_major: { value: new Vector4() },
        u_shadow: { value: new Vector2() }, u_shadow_a: { value: 0.3 }, u_below: { value: 0 },
      },
    });
    this.grid = new Mesh(new PlaneGeometry(2, 2), this.gridMat);
    this.grid.frustumCulled = false;
    this.grid.renderOrder = 1;

    const bgGeo = new BufferGeometry();
    bgGeo.setAttribute("position", new BufferAttribute(new Float32Array([-1, -1, 0, 3, -1, 0, -1, 3, 0]), 3));
    this.bgMat = new ShaderMaterial({
      vertexShader: BG_VS, fragmentShader: BG_FS, depthTest: false, depthWrite: false,
      uniforms: { u_top: { value: new Vector3() }, u_bottom: { value: new Vector3() } },
    });
    const bg = new Mesh(bgGeo, this.bgMat);
    bg.frustumCulled = false;
    bg.renderOrder = -10;

    this.scene.add(bg, this.mesh, this.grid);
  }

  /** Install the position buffer (9 floats per triangle). `onUploaded` fires once it is on the GPU. */
  setPositions(positions: Float32Array, bboxSize: readonly number[], onUploaded?: () => void): void {
    const attr = new BufferAttribute(positions, 3);
    if (onUploaded) {
      attr.onUpload(() => { attr.onUploadCallback = () => {}; onUploaded(); });
    }
    this.position = attr;
    this.geometry.setAttribute("position", attr);
    const r = Math.hypot(bboxSize[0]!, bboxSize[1]!, bboxSize[2]!) / 2 || 1;
    this.geometry.boundingSphere = new Sphere(new Vector3(), r);
  }

  /** Give the (transferred) positions array back after the worker is done with it. */
  restorePositions(positions: Float32Array): void {
    if (this.position) this.position.array = positions;
  }

  setNormals(normals: Int8Array): void {
    this.geometry.setAttribute("a_nrm", new Int8BufferAttribute(normals, 4, true));
    this.hasNormals = true;
  }

  setOrigin(o: Vec3): void { this.origin = o; }

  resize(width: number, height: number): void {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.renderer.setPixelRatio(dpr);
    this.renderer.setSize(Math.max(1, Math.floor(width)), Math.max(1, Math.floor(height)), false);
    this.camera.aspect = Math.max(width, 1) / Math.max(height, 1);
    this.camera.updateProjectionMatrix();
  }

  get size(): { w: number; h: number } {
    return { w: this.renderer.domElement.clientWidth, h: this.renderer.domElement.clientHeight };
  }

  render(cam: OrbitCamera, st: DisplayState): void {
    const pal = PALETTE[st.dark ? "dark" : "light"];
    const { w } = this.size;
    const px = this.renderer.domElement.width / Math.max(w, 1);
    const r = cam.radius;

    // camera pose from yaw / pitch / dist / target
    const { right, up, back } = cameraAxes(cam.yaw, cam.pitch);
    this.camera.position.set(
      cam.target[0] + back[0] * cam.dist, cam.target[1] + back[1] * cam.dist, cam.target[2] + back[2] * cam.dist);
    this.camera.quaternion.setFromRotationMatrix(
      new Matrix4().makeBasis(new Vector3(...right), new Vector3(...up), new Vector3(...back)));
    this.camera.near = Math.max(cam.dist - 8 * r, cam.dist * 0.01, r * 1e-3);
    this.camera.far = cam.dist + 8 * r;
    this.camera.updateProjectionMatrix();
    this.camera.updateMatrixWorld(true);

    // Y-up files are rotated into the Z-up world
    this.mesh.rotation.set(st.up === "y" ? Math.PI / 2 : 0, 0, 0);
    this.mesh.updateMatrixWorld(true);

    const u = this.meshMat.uniforms;
    u.u_flat!.value = st.flat || !this.hasNormals ? 1 : 0;
    u.u_wire!.value = st.wireframe ? 1 : 0;
    u.u_px!.value = px;
    u.u_sky!.value.set(...srgbToLinear(pal.sky));
    u.u_ground!.value.set(...srgbToLinear(pal.ground));
    u.u_up!.value.set(right[2], up[2], back[2]); // world up in view space
    u.u_wire_col!.value.set(...pal.wire);

    const bg = this.bgMat.uniforms;
    bg.u_top!.value.set(...pal.bgTop);
    bg.u_bottom!.value.set(...pal.bgBottom);

    this.grid.visible = st.grid;
    if (st.grid) {
      const step = gridStep(r);
      const g = this.gridMat.uniforms;
      g.u_extent!.value = Math.max(r * 2.4, step * 10);
      g.u_z!.value = -cam.half[2] - r * 1e-3;
      g.u_step!.value = step;
      // anchor the lines to real coordinates (the geometry is recentred on the bbox centre)
      const o = this.origin;
      const off = st.up === "y" ? [o[0], -o[2]] : [o[0], o[1]];
      g.u_offset!.value.set(off[0]!, off[1]!);
      g.u_px!.value = px;
      g.u_minor!.value.set(...pal.minor);
      g.u_major!.value.set(...pal.major);
      g.u_shadow!.value.set(cam.half[0] * 1.05 + r * 0.08, cam.half[1] * 1.05 + r * 0.08);
      g.u_shadow_a!.value = pal.shadow;
      g.u_below!.value = cam.pitch < -0.5 ? 1 : 0;
    }
    this.renderer.render(this.scene, this.camera);
  }

  /** Block until the GPU has finished the last frame (first-frame timing, tests). */
  finish(): void { this.renderer.getContext().finish(); }

  dispose(): void {
    this.geometry.dispose();
    this.meshMat.dispose();
    this.gridMat.dispose();
    this.bgMat.dispose();
    this.grid.geometry.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.canvas.remove();
  }
}
