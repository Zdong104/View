// Get Info sections for an image (image_ops.image_info_sections): Image, Camera,
// GPS (with an OpenStreetMap link) and Description. Header facts come from our
// own container parser; EXIF tags are decoded by exifr from the TIFF block we
// extracted (works the same for JPEG, PNG eXIf, WebP EXIF and TIFF).
import type { InfoSection } from "../contract.ts";
import { FMT_LABEL, iccDescription, inflate, parseContainer } from "./formats.ts";

export interface ImageFacts {
  /** Upright decoded size of the original. */
  size: [number, number] | null;
  edited: [number, number] | null;
  edits: string[];
  frames: number;
  /** Per-frame durations in ms when known. */
  durations: number[] | null;
  /** ImageDecoder repetitionCount: Infinity = forever, n = extra plays. */
  repetitions: number | null;
}

type Tags = Record<string, unknown>;
const num = (v: unknown): number | null => {
  if (typeof v === "number" && isFinite(v)) return v;
  if (Array.isArray(v) && typeof v[0] === "number") return v[0];
  return null;
};
const str = (v: unknown): string | null => {
  if (v === null || v === undefined || v === "") return null;
  if (v instanceof Uint8Array || (Array.isArray(v) && v.every((x) => typeof x === "number"))) {
    // XP* tags: UTF-16LE bytes
    const b = v instanceof Uint8Array ? v : Uint8Array.from(v as number[]);
    return new TextDecoder("utf-16le").decode(b).replace(/\0+$/, "").trim() || null;
  }
  if (v instanceof Date) return v.toLocaleString();
  return String(v).replace(/\0+$/, "").trim() || null;
};

function dms(v: unknown, ref: unknown): number | null {
  if (typeof v === "number") return /[SW]/i.test(String(ref)) ? -v : v;
  if (!Array.isArray(v) || v.length < 3) return null;
  const d = Number(v[0]) + Number(v[1]) / 60 + Number(v[2]) / 3600;
  if (!isFinite(d)) return null;
  return /[SW]/i.test(String(ref)) ? -d : d;
}

export async function parseExif(tiff: Uint8Array): Promise<{ ifd0: Tags; exif: Tags; gps: Tags }> {
  const exifr = (await import("exifr")).default;
  const out = await exifr.parse(tiff, {
    tiff: true, ifd0: true, exif: true, gps: true, ifd1: false, interop: false,
    xmp: false, icc: false, iptc: false, jfif: false, ihdr: false,
    translateKeys: true, translateValues: false, reviveValues: true, sanitize: true, mergeOutput: false,
  } as never) as { ifd0?: Tags; exif?: Tags; gps?: Tags } | undefined;
  return { ifd0: out?.ifd0 ?? {}, exif: out?.exif ?? {}, gps: out?.gps ?? {} };
}

export async function imageInfo(bytes: Uint8Array, f: ImageFacts): Promise<InfoSection[]> {
  const c = parseContainer(bytes);
  const img: InfoSection["rows"] = [];
  const cam: InfoSection["rows"] = [];
  const gps: InfoSection["rows"] = [];
  const desc: InfoSection["rows"] = [];
  const size = f.size ?? (c.width && c.height ? [c.width, c.height] : null);
  if (size) img.push(["Dimensions", `${size[0]} × ${size[1]} px`]);
  if (f.edits.length && f.edited) {
    img.push(["Edited size", `${f.edited[0]} × ${f.edited[1]} px`]);
    img.push(["Edits", f.edits.join(", ")]);
  }
  img.push(["Format", FMT_LABEL[c.fmt]]);

  let t: { ifd0: Tags; exif: Tags; gps: Tags } = { ifd0: {}, exif: {}, gps: {} };
  if (c.exif) t = await parseExif(c.exif).catch(() => t);
  const { ifd0, exif, gps: g } = t;

  let dpi = c.dpi ?? null;
  const xr = num(ifd0.XResolution);
  if (!dpi && xr && (num(ifd0.ResolutionUnit) ?? 2) === 2) dpi = [xr, num(ifd0.YResolution) ?? xr];
  if (dpi) img.push(["Resolution", `${Math.round(dpi[0])} × ${Math.round(dpi[1])} dpi`]);
  if (c.colorModel) img.push(["Color model", c.colorModel]);
  if (c.bitDepth) img.push(["Bit depth", String(c.bitDepth)]);
  if (c.alpha !== undefined) img.push(["Alpha channel", c.alpha ? "Yes" : "No"]);
  let prof = "None embedded";
  if (c.icc) {
    try {
      prof = iccDescription(c.iccCompressed ? await inflate(c.icc) : c.icc) ?? "Embedded profile";
    } catch { prof = "Embedded profile"; }
  }
  img.push(["Color profile", prof]);
  if (f.frames > 1) {
    img.push(["Frames", `${f.frames} (animated)`]);
    if (f.durations) img.push(["Duration", `${(f.durations.reduce((a, b) => a + b, 0) / 1000).toFixed(2)} s`]);
    const r = f.repetitions;
    img.push(["Loop", r === null ? "Forever" : r === Infinity ? "Forever" : r === 0 ? "Once" : `${r + 1} times`]);
  }
  const orient = num(ifd0.Orientation);
  if (orient && orient !== 1) img.push(["EXIF orientation", String(orient)]);

  const put = (rows: InfoSection["rows"], label: string, v: unknown) => { const s = str(v); if (s) rows.push([label, s]); };
  put(cam, "Make", ifd0.Make);
  put(cam, "Model", ifd0.Model);
  put(cam, "Lens", exif.LensModel);
  const fl = num(exif.FocalLength);
  if (fl) {
    const f35 = num(exif.FocalLengthIn35mmFormat);
    cam.push(["Focal length", `${+fl.toFixed(2)} mm${f35 ? ` (${f35} mm equiv.)` : ""}`]);
  }
  const fn = num(exif.FNumber);
  if (fn) cam.push(["Aperture", `f/${fn.toFixed(1).replace(/\.0$/, "")}`]);
  const et = num(exif.ExposureTime);
  if (et) cam.push(["Exposure", et < 1 ? `1/${Math.round(1 / et)} s` : `${et} s`]);
  const iso = num(exif.ISO ?? exif.ISOSpeedRatings ?? exif.PhotographicSensitivity);
  if (iso) cam.push(["ISO", String(iso)]);
  const bias = num(exif.ExposureCompensation ?? exif.ExposureBiasValue);
  if (bias !== null) cam.push(["Exposure bias", `${bias >= 0 ? "+" : ""}${bias.toFixed(1)} EV`]);
  const flash = num(exif.Flash);
  if (flash !== null) cam.push(["Flash", flash & 1 ? "Fired" : "Did not fire"]);
  put(cam, "Date taken", exif.DateTimeOriginal ?? ifd0.ModifyDate ?? ifd0.DateTime);
  put(cam, "Software", ifd0.Software);
  put(cam, "Artist", ifd0.Artist);
  put(cam, "Copyright", ifd0.Copyright);

  const lat = dms(g.GPSLatitude, g.GPSLatitudeRef), lon = dms(g.GPSLongitude, g.GPSLongitudeRef);
  if (lat !== null && lon !== null) {
    gps.push(["Coordinates", `${lat.toFixed(6)}, ${lon.toFixed(6)}`]);
    const alt = num(g.GPSAltitude);
    if (alt !== null) gps.push(["Altitude", `${Math.round(num(g.GPSAltitudeRef) === 1 ? -alt : alt)} m`]);
    const a = lat.toFixed(6), o = lon.toFixed(6);
    gps.push(["Map", `[Open in Maps](https://www.openstreetmap.org/?mlat=${a}&mlon=${o}#map=15/${a}/${o})`]);
  }

  put(desc, "Title", ifd0.XPTitle);
  put(desc, "Description", ifd0.ImageDescription ?? ifd0.XPComment);
  put(desc, "Keywords", ifd0.XPKeywords);

  const out: InfoSection[] = [{ title: "Image", rows: img }];
  for (const [title, rows] of [["Camera", cam], ["GPS", gps], ["Description", desc]] as const) {
    if (rows.length) out.push({ title, rows: [...rows] });
  }
  return out;
}
