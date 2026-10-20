/* View table worker (classic worker; copied to dist/vendor/table/worker.js by copy.json).
 *
 * Owns the document model of the table viewer (port of preview/table_data.py):
 *   - the file stays in memory as raw bytes; a Uint32Array holds each record's start offset
 *     (a quote-aware scan, no per-row objects), rows are parsed lazily with PapaParse;
 *   - rows are identified by keys: base rows by record index, new rows by nbase + n;
 *     `order` is the document order, `view` the displayed order (sorted / filtered),
 *     `edits` maps key -> full replacement row (nothing is copied per cell edit);
 *   - undo/redo with row deltas and array snapshots (arrays are replaced, never mutated);
 *   - saving copies unmodified runs of the original bytes verbatim (Blob.slice), so
 *     quoting, line endings, BOM and encoding of untouched rows are preserved exactly.
 *
 * Plain JS on purpose: it is not bundled, only copied. Protocol: {id, op, ...args} in,
 * {id, ok, result|error} out, plus {event: "state"|"progress"|"scan", ...}.
 */
/* global Papa */
"use strict";
if (typeof importScripts === "function") importScripts("papaparse.min.js");

const DELIMS = [",", "\t", ";", "|"];
const DELIM_NAMES = { ",": "Comma", "\t": "Tab", ";": "Semicolon", "|": "Pipe" };
const CUR = "$€£¥";
const FIRST_SLICE = 512 * 1024;
const SLICE = 6 * 1024 * 1024;
const UNDO_BYTES = 256 * 1024 * 1024;
const yieldNow = () => new Promise((r) => setTimeout(r, 0));

// ---------------------------------------------------------------------------
// small helpers (ports of table_data.py)
// ---------------------------------------------------------------------------
function toNum(s) {
  s = s.trim();
  if (!s) return null;
  const c = s[0];
  if (!((c >= "0" && c <= "9") || "+-.(".includes(c) || CUR.includes(c))) return null;
  if (/^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(s)) return parseFloat(s);
  let t = s, neg = false;
  if (t.startsWith("(") && t.endsWith(")")) { neg = true; t = t.slice(1, -1); }
  t = t.trim();
  while (t && CUR.includes(t[0])) t = t.slice(1);
  t = t.replace(/,/g, "").replace(/%+$/, "").trim();
  if (!t || !((t[0] >= "0" && t[0] <= "9") || "+-.".includes(t[0]))) return null;
  if (!/^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(t)) return null;
  const v = parseFloat(t);
  return neg ? -v : v;
}

const DATE_RES = [
  /^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?)?$/,
  /^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}(\s+\d{1,2}:\d{2}(:\d{2})?)?$/,
  /^\d{4}年\d{1,2}月\d{1,2}日?$/,
  /^[A-Za-z]{3,9}\.? \d{1,2},? \d{4}$/,
  /^\d{1,2} [A-Za-z]{3,9}\.? \d{4}$/,
];
const isDate = (s) => { s = s.trim(); return !!s && DATE_RES.some((r) => r.test(s)); };

function inferType(values) {
  let n = 0, num = 0, dt = 0;
  for (const v of values) {
    if (!v || !v.trim()) continue;
    n++;
    if (toNum(v) !== null) num++;
    else if (isDate(v)) dt++;
  }
  if (n === 0) return "empty";
  if (num >= 0.9 * n) return "number";
  if (dt >= 0.9 * n) return "date";
  return "text";
}

const encLabel = (e) => ({
  "utf-8": "UTF-8", "windows-1252": "Windows-1252", "iso-8859-1": "Latin-1", "gb18030": "GB18030",
  "big5": "Big5", "shift_jis": "Shift-JIS", "euc-kr": "EUC-KR", "utf-16le": "UTF-16 LE",
  "utf-16be": "UTF-16 BE",
}[e] || e);

// ---------------------------------------------------------------------------
// document state
// ---------------------------------------------------------------------------
let D = null;
let opToken = 0; // bumped by sort/search to cancel running ones

function newDoc() {
  return {
    blob: null, data: new Uint8Array(0), name: "",
    enc: "utf-8",          // encoding of D.data (always ASCII compatible)
    outEnc: "utf-8",       // encoding written on save (differs for UTF-16)
    transcoded: false,     // D.data is a UTF-8 transcode of a UTF-16 file
    bom: new Uint8Array(0), baseOff: 0, decoder: null,
    delim: ",", quote: '"', quoting: "minimal", outQuoting: "minimal",
    eol: "\n", outEol: "\n", scanNl: 10,
    hasHeader: false, headerGuess: false, endsWithNewline: true, fileSize: 0,
    starts: new Uint32Array(1), nbase: 0, maxDelims: 0, loaded: false, scanDone: false,
    voff: 0, headerKey: -1, ncols: 1, baseNcols: 1, colmap: [0], headers: [""], origHeaders: [""],
    edits: new Map(), nextNew: 0,
    order: new Int32Array(0), view: null,
    sortCol: null, sortAsc: true, filterOn: false,
    search: "", matchBits: null, matchNew: new Set(), matchVpos: new Int32Array(0),
    rowCache: new Map(),
    undo: [], redo: [], undoBytes: 0, group: null, savedMarker: null, pendingMarker: null,
    epoch: 0, struct: 0, scanning: false, error: null,
  };
}

const post = (m, transfer) => self.postMessage(m, transfer || []);
const colIdentity = () => D.colmap.length === D.baseNcols && D.colmap.every((v, i) => v === i);
const viewLen = () => (D.view ? D.view.length : Math.max(0, D.nbase - D.voff));
const viewKey = (p) => (D.view ? D.view[p] : p + D.voff);

function stateOf() {
  const top = D.undo.length ? D.undo[D.undo.length - 1] : null;
  return {
    loaded: D.loaded, scanning: D.scanning, nView: viewLen(), nbase: D.nbase, ncols: D.ncols,
    hasHeader: D.hasHeader, headers: D.headers, delim: D.delim,
    delimName: DELIM_NAMES[D.delim] || JSON.stringify(D.delim),
    encoding: D.outEnc, encLabel: D.transcoded ? encLabel(D.outEnc) + (D.bom.length ? " (BOM)" : "")
      : D.bom.length ? "UTF-8 (BOM)" : encLabel(D.enc),
    eol: D.eol, outEol: D.outEol, quoting: D.quoting, outQuoting: D.outQuoting,
    dirty: top !== D.savedMarker, canUndo: D.undo.length > 0, canRedo: D.redo.length > 0,
    undoLabel: D.undo.length ? D.undo[D.undo.length - 1].label : "",
    redoLabel: D.redo.length ? D.redo[D.redo.length - 1].label : "",
    sortCol: D.sortCol, sortAsc: D.sortAsc, filterOn: D.filterOn, search: D.search,
    matches: D.matchVpos.length, epoch: D.epoch, struct: D.struct,
    fileSize: D.fileSize, name: D.name, endsWithNewline: D.endsWithNewline,
  };
}
const emitState = () => post({ event: "state", state: stateOf() });

// ---------------------------------------------------------------------------
// encoding + dialect detection
// ---------------------------------------------------------------------------
function validUtf8(data) {
  // Pure ASCII (the common case) needs no decoding: check 4 bytes at a time.
  const words = data.byteLength >> 2;
  if (data.byteOffset % 4 === 0) {
    const w32 = new Uint32Array(data.buffer, data.byteOffset, words);
    let i = 0;
    while (i < words && (w32[i] & -2139062144) === 0) i++;
    if (i === words) {
      let ascii = true;
      for (let j = words << 2; j < data.length; j++) if (data[j] >= 0x80) { ascii = false; break; }
      if (ascii) return true;
    }
  }
  const dec = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
  const step = 8 << 20;
  try {
    for (let i = 0; i < data.length; i += step) dec.decode(data.subarray(i, Math.min(data.length, i + step)), { stream: true });
    dec.decode();
    return true;
  } catch { return false; }
}

function decodeFatal(label, bytes) {
  try { new TextDecoder(label, { fatal: true }).decode(bytes); return true; } catch { return false; }
}

function guessLegacy(data) {
  const sample = data.subarray(0, Math.min(data.length, 256 * 1024));
  let hi = 0, paired = 0;
  for (let i = 0; i < sample.length; i++) {
    if (sample[i] >= 0x80) {
      hi++;
      if (i + 1 < sample.length && sample[i + 1] >= 0x40) { paired++; i++; }
    }
  }
  if (hi > 0 && paired >= 0.9 * hi) {
    const cut = sample.subarray(0, Math.max(0, sample.length - 4));
    for (const label of ["gb18030", "big5", "shift_jis", "euc-kr"]) if (decodeFatal(label, cut)) return label;
  }
  return "windows-1252";
}

function detectEncoding(forced) {
  const data = D.data;
  const f = (forced || "").toLowerCase();
  const head = data.subarray(0, 4);
  D.baseOff = 0; D.bom = new Uint8Array(0); D.transcoded = false;
  const setUtf16 = (le, bomLen) => {
    const label = le ? "utf-16le" : "utf-16be";
    const text = new TextDecoder(label, { ignoreBOM: true }).decode(data.subarray(bomLen));
    D.data = new TextEncoder().encode(text);
    D.bom = bomLen ? data.slice(0, bomLen) : new Uint8Array(0);
    D.enc = "utf-8"; D.outEnc = label; D.transcoded = true;
  };
  if (head[0] === 0xEF && head[1] === 0xBB && head[2] === 0xBF && (!f || f === "utf-8" || f === "utf-8-sig")) {
    D.bom = data.slice(0, 3); D.baseOff = 3; D.enc = D.outEnc = "utf-8"; return;
  }
  if (head[0] === 0xFF && head[1] === 0xFE && (!f || f.startsWith("utf-16"))) return setUtf16(true, 2);
  if (head[0] === 0xFE && head[1] === 0xFF && (!f || f.startsWith("utf-16"))) return setUtf16(false, 2);
  if (f === "utf-16" || f === "utf-16le") return setUtf16(true, 0);
  if (f === "utf-16be") return setUtf16(false, 0);
  if (f === "utf-8-sig") { D.bom = new Uint8Array([0xEF, 0xBB, 0xBF]); D.enc = D.outEnc = "utf-8"; return; }
  if (f) { new TextDecoder(f); D.enc = D.outEnc = f; return; }
  if (validUtf8(data)) { D.enc = D.outEnc = "utf-8"; return; }
  D.enc = D.outEnc = guessLegacy(data);
}

function detectDialect(opts) {
  const data = D.data, s0 = D.baseOff;
  let raw = data.subarray(s0, Math.min(data.length, s0 + 200000));
  const count = (buf) => {
    let crlf = 0, lf = 0, cr = 0;
    for (let i = 0; i < buf.length; i++) {
      const b = buf[i];
      if (b === 10) { lf++; if (i > 0 && buf[i - 1] === 13) crlf++; } else if (b === 13) cr++;
    }
    return { crlf, lf, cr: cr - crlf };
  };
  let c = count(raw);
  if (c.lf === 0 && c.cr === 0 && data.length - s0 > raw.length) c = count(data.subarray(s0, Math.min(data.length, s0 + 4000000)));
  if (c.lf === 0 && c.cr > 0) { D.eol = "\r"; D.scanNl = 13; }
  else if (c.crlf && c.crlf >= c.lf * 0.5) { D.eol = "\r\n"; D.scanNl = 10; }
  else { D.eol = "\n"; D.scanNl = 10; }
  D.outEol = D.eol;
  const last = data.length ? data[data.length - 1] : 10;
  D.endsWithNewline = !data.length || last === 10 || (D.scanNl === 13 && last === 13);
  // decode a sample cut at a record boundary
  if (data.length - s0 > raw.length) {
    const cut = raw.lastIndexOf(D.scanNl);
    if (cut > 0) raw = raw.subarray(0, cut + 1);
  }
  const sample = new TextDecoder(D.enc, { ignoreBOM: true }).decode(raw);
  const ext = (D.name.match(/\.([^.]+)$/) || [, ""])[1].toLowerCase();
  D.delim = opts.delimiter || pickDelim(sample, ext);
  const lines = sample.split(/\r\n|\n|\r/).filter((l) => l.trim()).slice(0, 5);
  let allq = lines.length > 0;
  for (const l of lines) {
    const fields = l.split(D.delim);
    if (!fields.every((x) => x.length >= 2 && x.startsWith('"') && x.endsWith('"'))) { allq = false; break; }
  }
  D.quoting = D.outQuoting = allq ? "all" : "minimal";
  D.headerGuess = guessHeader(sample);
  D.hasHeader = opts.header === undefined || opts.header === null ? D.headerGuess : !!opts.header;
}

function parseText(text, delim, preview) {
  const r = Papa.parse(text, { delimiter: delim, quoteChar: '"', escapeChar: '"', newline: "", preview: preview || 0, skipEmptyLines: false });
  return r.data;
}

function pickDelim(sample, ext) {
  let best = ",", bestScore = -1;
  const text = sample.slice(0, 120000);
  for (const d of DELIMS) {
    let rows;
    try { rows = parseText(text, d, 200).filter((r) => !(r.length === 1 && r[0] === "")).map((r) => r.length); } catch { continue; }
    if (!rows.length) continue;
    const cnt = new Map();
    for (const n of rows) cnt.set(n, (cnt.get(n) || 0) + 1);
    let mode = 0, freq = 0;
    for (const [n, f] of cnt) if (f > freq || (f === freq && n > mode)) { mode = n; freq = f; }
    let score = mode > 1 ? freq / rows.length : 0;
    if (mode > 1) score += Math.min(mode, 20) * 0.002;
    if ((ext === "tsv" || ext === "tab") && d === "\t") score += 0.3;
    else if (ext === "csv" && d === ",") score += 0.05;
    if (score > bestScore) { best = d; bestScore = score; }
  }
  return best;
}

function guessHeader(sample) {
  let rows;
  try { rows = parseText(sample.slice(0, 60000), D.delim, 60).filter((r) => !(r.length === 1 && r[0] === "")); } catch { return true; }
  if (!rows.length) return false;
  const first = rows[0].map((c) => c.trim());
  const filled = first.filter(Boolean);
  if (!filled.length || filled.length < 0.6 * first.length) return false;
  if (filled.filter((c) => toNum(c) !== null).length > 0.3 * filled.length) return false;
  if (new Set(filled).size !== filled.length) return false;
  if (rows.length > 1) {
    for (let c = 0; c < rows[0].length; c++) {
      const col = rows.slice(1).map((r) => r[c]).filter((v) => v !== undefined && v.trim());
      if (col.length >= 3 && col.filter((v) => toNum(v) !== null).length >= 0.9 * col.length && toNum(first[c]) === null) return true;
    }
  }
  return first.length > 1;
}

// ---------------------------------------------------------------------------
// record index (quote-aware scan) and row parsing
// ---------------------------------------------------------------------------
async function scanAll(token, onFirst) {
  const data = D.data, n = data.length;
  const NL = D.scanNl, Q = D.quote.charCodeAt(0), DL = D.delim.charCodeAt(0);
  let starts = new Uint32Array(Math.max(1024, Math.min(n, n >> 4)));
  let cnt = 0, pos = D.baseOff, st = 1, delims = 0, maxD = 0;
  starts[0] = pos;
  let first = true;
  D.scanning = true;
  while (pos < n) {
    const end = Math.min(n, pos + (first ? FIRST_SLICE : SLICE));
    for (; pos < end; pos++) {
      const b = data[pos];
      let rec = false;
      switch (st) {
        case 1: // at field start
          if (b === Q) st = 2;
          else if (b === DL) delims++;
          else if (b === NL) rec = true;
          else st = 0;
          break;
        case 0: // inside an unquoted field
          if (b === DL) { st = 1; delims++; } else if (b === NL) rec = true;
          break;
        case 2: // inside quotes
          if (b === Q) st = 3;
          break;
        default: // 3: a quote inside quotes: escaped quote, or the closing one
          if (b === Q) st = 2;
          else if (b === DL) { st = 1; delims++; } else if (b === NL) rec = true;
          else st = 0;
      }
      if (rec) {
        if (++cnt >= starts.length - 1) { const g = new Uint32Array(starts.length * 2); g.set(starts); starts = g; }
        starts[cnt] = pos + 1;
        if (delims > maxD) maxD = delims;
        delims = 0; st = 1;
      }
    }
    if (pos >= n && pos > starts[cnt]) { // last record without a terminator
      cnt++; if (cnt >= starts.length) { const g = new Uint32Array(starts.length + 2); g.set(starts); starts = g; }
      starts[cnt] = n;
      if (delims > maxD) maxD = delims;
      D.endsWithNewline = false;
    }
    if (token !== D.token) return;
    D.starts = starts; D.nbase = cnt; D.maxDelims = maxD;
    if (first) {
      first = false;
      initColumns();
      onFirst && onFirst();
    } else {
      growColumns();
      post({ event: "scan", nbase: D.nbase, ncols: D.ncols, frac: pos / n });
    }
    if (pos < n) await yieldNow();
  }
  D.scanning = false;
  D.scanDone = true;
  finishLoad();
}

function initColumns() {
  D.baseNcols = D.ncols = Math.max(1, D.maxDelims + 1);
  D.colmap = Array.from({ length: D.ncols }, (_, i) => i);
  D.voff = D.hasHeader && D.nbase > 0 ? 1 : 0;
  D.headerKey = D.voff ? 0 : -1;
  const hdr = D.voff ? parseRecords(0, 1)[0] : [];
  D.headers = Array.from({ length: D.ncols }, (_, i) => hdr[i] || "");
  D.origHeaders = D.headers.slice();
  D.rowCache.clear();
}

/** Columns found by later scan slices (only before the user can change columns). */
function growColumns() {
  const want = Math.max(1, D.maxDelims + 1);
  if (want > D.ncols && !D.loaded) {
    for (let i = D.ncols; i < want; i++) { D.colmap.push(i); D.headers.push(""); D.origHeaders.push(""); }
    D.baseNcols = D.ncols = want;
    D.rowCache.clear();
  }
}

function finishLoad() {
  growColumns();
  D.order = new Int32Array(Math.max(0, D.nbase - D.voff));
  for (let i = 0; i < D.order.length; i++) D.order[i] = i + D.voff;
  D.view = D.order;
  D.loaded = true;
  D.savedMarker = null;
  D.epoch++; // the view is the identity order, so row positions did not move: no struct bump
  emitState();
}

/** Parse records [i, j) into arrays of strings (base columns, unmapped). */
function parseRecords(i, j) {
  if (j <= i) return [];
  const dec = D.decoder;
  const text = dec.decode(D.data.subarray(D.starts[i], D.starts[j]));
  const want = j - i;
  let rows = parseBlockText(text);
  if (rows.length === want + 1 && rows[want].length === 1 && rows[want][0] === "") rows.pop();
  if (rows.length === want) return rows;
  rows = [];
  for (let k = i; k < j; k++) rows.push(parseOne(k));
  return rows;
}

function stripEol(t) {
  if (t.endsWith("\r\n")) return t.slice(0, -2);
  if (t.endsWith("\n") || t.endsWith("\r")) return t.slice(0, -1);
  return t;
}

function parseOne(k) {
  const t = stripEol(D.decoder.decode(D.data.subarray(D.starts[k], D.starts[k + 1])));
  if (t.indexOf(D.quote) < 0) return t.split(D.delim);
  const r = Papa.parse(t, { delimiter: D.delim, quoteChar: D.quote, escapeChar: D.quote, newline: D.scanNl === 13 ? "\r" : "\n", skipEmptyLines: false });
  return r.data.length ? r.data[0] : [""];
}

function parseBlockText(text) {
  const nl = D.scanNl === 13 ? "\r" : "\n";
  if (text.indexOf(D.quote) < 0) {
    const lines = text.split(nl);
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    const crlf = D.eol === "\r\n";
    const out = new Array(lines.length);
    for (let i = 0; i < lines.length; i++) {
      let l = lines[i];
      if (crlf && l.charCodeAt(l.length - 1) === 13) l = l.slice(0, -1);
      out[i] = l.split(D.delim);
    }
    return out;
  }
  const r = Papa.parse(text, { delimiter: D.delim, quoteChar: D.quote, escapeChar: D.quote, newline: D.eol, skipEmptyLines: false });
  return r.data;
}

function mapCols(r) {
  const n = D.ncols;
  if (D.colmap.length === D.baseNcols && D.colmap.every((v, i) => v === i)) {
    if (r.length === n) return r;
    if (r.length < n) { const o = r.slice(); while (o.length < n) o.push(""); return o; }
    return r.slice(0, n);
  }
  const o = new Array(n);
  for (let c = 0; c < n; c++) { const s = D.colmap[c]; o[c] = s >= 0 && s < r.length ? r[s] : ""; }
  return o;
}

const isIdentityCols = () => D.colmap.length === D.baseNcols && D.ncols === D.baseNcols && colIdentity();

/** Rows of the given keys (current columns, edits applied). Batches consecutive base keys. */
function rowsOfKeys(keys) {
  const out = new Array(keys.length);
  const ident = isIdentityCols();
  let i = 0;
  while (i < keys.length) {
    const k = keys[i];
    const e = D.edits.get(k);
    if (e) { out[i] = e; i++; continue; }
    if (k >= D.nbase || k < 0) { out[i] = new Array(D.ncols).fill(""); i++; continue; }
    const c = D.rowCache.get(k);
    if (c) { out[i] = c; i++; continue; }
    let j = i + 1;
    while (j < keys.length && keys[j] === keys[j - 1] + 1 && keys[j] < D.nbase && !D.edits.has(keys[j]) && !D.rowCache.has(keys[j])) j++;
    const parsed = parseRecords(k, k + (j - i));
    if (D.rowCache.size > 30000) D.rowCache.clear();
    for (let m = 0; m < parsed.length; m++) {
      const r = ident && parsed[m].length === D.ncols ? parsed[m] : mapCols(parsed[m]);
      D.rowCache.set(k + m, r);
      out[i + m] = r;
    }
    i = j;
  }
  return out;
}

function viewRows(start, count) {
  const end = Math.min(viewLen(), start + count);
  const keys = [];
  for (let p = Math.max(0, start); p < end; p++) keys.push(viewKey(p));
  return rowsOfKeys(keys);
}

// ---------------------------------------------------------------------------
// undo / redo
// ---------------------------------------------------------------------------
function push(cmd) {
  if (D.group) { D.group.push(cmd); return; }
  D.undo.push(cmd);
  D.undoBytes += cmd.nbytes || 0;
  D.redo.length = 0;
  while (D.undoBytes > UNDO_BYTES && D.undo.length > 1) {
    const old = D.undo.shift();
    D.undoBytes -= old.nbytes || 0;
    if (D.savedMarker === old) D.savedMarker = undefined; // can no longer return to the saved state
    if (D.pendingMarker === old) D.pendingMarker = undefined;
  }
}

function withGroup(label, fn) {
  const prev = D.group;
  D.group = [];
  try { fn(); } finally {
    const cmds = D.group;
    D.group = prev;
    if (cmds.length) {
      const nbytes = cmds.reduce((a, c) => a + (c.nbytes || 0), 0);
      push({ label, nbytes,
        undo() { for (let i = cmds.length - 1; i >= 0; i--) cmds[i].undo(); },
        redo() { for (const c of cmds) c.redo(); } });
    }
  }
}

function changed(struct) {
  D.epoch++;
  if (struct) { D.struct++; D.rowCache.clear(); recomputeMatchPositions(); }
}

function doUndo() {
  const c = D.undo.pop();
  if (!c) return;
  c.undo(); D.undoBytes -= c.nbytes || 0; D.redo.push(c);
  changed(true);
}
function doRedo() {
  const c = D.redo.pop();
  if (!c) return;
  c.redo(); D.undo.push(c); D.undoBytes += c.nbytes || 0;
  changed(true);
}

// ---------------------------------------------------------------------------
// edits
// ---------------------------------------------------------------------------
function blankRow() { return new Array(D.ncols).fill(""); }

function setCells(changes, label) {
  // changes: [vrow, col, text]
  const byKey = new Map();
  for (const [vr, c, t] of changes) {
    if (vr < 0 || vr >= viewLen() || c < 0 || c >= D.ncols) continue;
    const k = viewKey(vr);
    let list = byKey.get(k);
    if (!list) byKey.set(k, (list = []));
    list.push([c, t]);
  }
  if (!byKey.size) return;
  const keys = [...byKey.keys()];
  const before = rowsOfKeys(keys);
  const deltas = [];
  keys.forEach((k, i) => {
    const prev = D.edits.get(k);
    const row = before[i].slice();
    let any = false;
    for (const [c, t] of byKey.get(k)) if (row[c] !== t) { row[c] = t; any = true; }
    if (any) deltas.push({ k, prev, next: row });
  });
  if (!deltas.length) return;
  const apply = (which) => {
    for (const d of deltas) {
      const v = which === "undo" ? d.prev : d.next;
      if (v === undefined) D.edits.delete(d.k); else D.edits.set(d.k, v);
      D.rowCache.delete(d.k);
    }
  };
  apply("redo");
  const nbytes = deltas.length * 200;
  push({ label: label || "Edit Cells", nbytes, undo: () => apply("undo"), redo: () => apply("redo") });
  changed(false);
}

function structCmd(label, before, after, nbytes) {
  push({ label, nbytes, undo: () => restore(before), redo: () => restore(after) });
}
function snap() {
  return { order: D.order, view: D.view, colmap: D.colmap, headers: D.headers, ncols: D.ncols, edits: D.edits,
    hasHeader: D.hasHeader, headerKey: D.headerKey, sortCol: D.sortCol, sortAsc: D.sortAsc, filterOn: D.filterOn,
    outEol: D.outEol, outQuoting: D.outQuoting };
}
function restore(s) {
  Object.assign(D, s);
  D.rowCache.clear();
}
function snapBytes(...ss) {
  let b = 0;
  for (const s of ss) b += (s.order.byteLength + (s.view && s.view !== s.order ? s.view.byteLength : 0));
  return b;
}

function insertRows(vpos, count) {
  vpos = Math.max(0, Math.min(vpos, viewLen()));
  if (count < 1) return [];
  const before = snap();
  const keys = new Int32Array(count);
  for (let i = 0; i < count; i++) keys[i] = D.nbase + D.nextNew++;
  const splice = (arr, at) => {
    const out = new Int32Array(arr.length + count);
    out.set(arr.subarray(0, at)); out.set(keys, at); out.set(arr.subarray(at), at + count);
    return out;
  };
  // position in document order = that of the row currently at vpos (or the end)
  let opos = D.order.length;
  if (vpos < D.view.length) { const at = D.order.indexOf(D.view[vpos]); if (at >= 0) opos = at; }
  const sameArr = D.view === D.order;
  const newOrder = splice(D.order, opos);
  D.order = newOrder;
  D.view = sameArr ? newOrder : splice(D.view, vpos);
  structCmd("Insert Rows", before, snap(), snapBytes(before));
  changed(true);
  return Array.from(keys);
}

function deleteRows(ranges) {
  const n = viewLen();
  const del = new Uint8Array(n);
  let total = 0;
  for (const [a, b] of ranges) for (let p = Math.max(0, a); p <= Math.min(n - 1, b); p++) if (!del[p]) { del[p] = 1; total++; }
  if (!total) return 0;
  const before = snap();
  const keySet = new Set();
  for (let p = 0; p < n; p++) if (del[p]) keySet.add(D.view[p]);
  const keep = (arr) => arr.filter((k) => !keySet.has(k));
  const sameArr = D.view === D.order;
  const newOrder = keep(D.order);
  D.order = newOrder;
  D.view = sameArr ? newOrder : keep(D.view);
  structCmd("Delete Rows", before, snap(), snapBytes(before));
  changed(true);
  return total;
}

function mapEdits(fn) {
  const out = new Map();
  for (const [k, r] of D.edits) out.set(k, fn(r));
  return out;
}

function insertCols(at, count) {
  at = Math.max(0, Math.min(at, D.ncols));
  if (count < 1) return;
  const before = snap();
  const ins = new Array(count).fill(-1);
  D.colmap = [...D.colmap.slice(0, at), ...ins, ...D.colmap.slice(at)];
  D.headers = [...D.headers.slice(0, at), ...new Array(count).fill(""), ...D.headers.slice(at)];
  D.edits = mapEdits((r) => [...r.slice(0, at), ...new Array(count).fill(""), ...r.slice(at)]);
  D.ncols += count;
  structCmd("Insert Columns", before, snap(), D.edits.size * 100);
  changed(true);
}

function deleteCols(cols) {
  const set = new Set(cols.filter((c) => c >= 0 && c < D.ncols));
  if (!set.size || set.size >= D.ncols) return false;
  const before = snap();
  const keepIdx = [];
  for (let c = 0; c < D.ncols; c++) if (!set.has(c)) keepIdx.push(c);
  D.colmap = keepIdx.map((c) => D.colmap[c]);
  D.headers = keepIdx.map((c) => D.headers[c]);
  D.edits = mapEdits((r) => keepIdx.map((c) => r[c]));
  D.ncols = keepIdx.length;
  if (D.sortCol !== null) D.sortCol = set.has(D.sortCol) ? null : keepIdx.indexOf(D.sortCol);
  structCmd("Delete Columns", before, snap(), D.edits.size * 100);
  changed(true);
  return true;
}

function setHeader(col, text) {
  if (col < 0 || col >= D.ncols || D.headers[col] === text) return;
  const before = snap();
  D.headers = D.headers.slice(); D.headers[col] = text;
  structCmd("Rename Column", before, snap(), 64);
  changed(false);
}

function setHasHeader(flag) {
  flag = !!flag;
  if (flag === D.hasHeader) return;
  const before = snap();
  const sameArr = D.view === D.order;
  if (flag) {
    // the first row of the document becomes the header
    const first = D.order.length ? D.order[0] : -1;
    D.hasHeader = true;
    if (first >= 0) {
      D.headers = rowsOfKeys([first])[0].slice();
      D.headerKey = first;
      D.order = D.order.slice(1);
      D.view = sameArr ? D.order : D.view.filter((k) => k !== first);
    }
  } else {
    // the header goes back into the data as the first row (raw again if it was not renamed)
    let key = D.headerKey;
    if (key < 0 || !headerIsRaw()) {
      key = D.nbase + D.nextNew++;
      D.edits = new Map(D.edits);
      D.edits.set(key, D.headers.slice());
    }
    const ins = (arr) => { const o = new Int32Array(arr.length + 1); o[0] = key; o.set(arr, 1); return o; };
    D.order = ins(D.order);
    D.view = sameArr ? D.order : ins(D.view);
    D.headers = new Array(D.ncols).fill("");
    D.hasHeader = false;
    D.headerKey = -1;
  }
  structCmd(flag ? "Use First Row as Header" : "Header Row to Data", before, snap(), snapBytes(before));
  changed(true);
}

/** The header is still byte-identical to its record in the file. */
function headerIsRaw() {
  const k = D.headerKey;
  if (k < 0 || k >= D.nbase || D.edits.has(k)) return false;
  const row = rowsOfKeys([k])[0];
  return row.length === D.headers.length && row.every((v, i) => v === D.headers[i]);
}

// ---------------------------------------------------------------------------
// sorting
// ---------------------------------------------------------------------------
async function columnValues(keys, col, token, progress) {
  const out = new Array(keys.length);
  const identity = isIdentityCols();
  const src = D.colmap[col];
  let i = 0, done = 0;
  while (i < keys.length) {
    if (token !== opToken) return null;
    const k = keys[i];
    if (D.edits.has(k) || k >= D.nbase) { out[i] = (D.edits.get(k) || [])[col] ?? ""; i++; continue; }
    let j = i + 1;
    while (j < keys.length && keys[j] === keys[j - 1] + 1 && keys[j] < D.nbase && !D.edits.has(keys[j]) && j - i < 50000) j++;
    if (src < 0) { for (let m = i; m < j; m++) out[m] = ""; }
    else extractColumn(k, k + (j - i), src, out, i);
    i = j;
    done += 1;
    if (done % 8 === 0) { progress && progress(i / keys.length); await yieldNow(); }
  }
  return out;
}

/** Column `col` (base index) of records [a, b) into out[at...]. */
function extractColumn(a, b, col, out, at) {
  const text = D.decoder.decode(D.data.subarray(D.starts[a], D.starts[b]));
  if (text.indexOf(D.quote) < 0) {
    const nl = D.scanNl === 13 ? "\r" : "\n", dl = D.delim;
    let p = 0, n = 0;
    const want = b - a;
    while (n < want) {
      let e = text.indexOf(nl, p);
      if (e < 0) e = text.length;
      let lineEnd = e;
      if (D.eol === "\r\n" && lineEnd > p && text.charCodeAt(lineEnd - 1) === 13) lineEnd--;
      let s = p, c = col;
      while (c > 0) {
        const d = text.indexOf(dl, s);
        if (d < 0 || d >= lineEnd) { s = -1; break; }
        s = d + 1; c--;
      }
      if (s < 0) out[at + n] = "";
      else {
        let d = text.indexOf(dl, s);
        if (d < 0 || d > lineEnd) d = lineEnd;
        out[at + n] = text.slice(s, d);
      }
      n++; p = e + 1;
    }
    return;
  }
  const rows = parseRecords(a, b);
  for (let m = 0; m < rows.length; m++) out[at + m] = rows[m][col] ?? "";
}

async function sortedKeys(keys, col, asc, token, progress) {
  const vals = await columnValues(keys, col, token, progress);
  if (!vals) return null;
  const n = keys.length;
  const cls = new Uint8Array(n); // 0 number, 1 text, 2 empty
  const sec = new Float64Array(n);
  const textIdx = [];
  for (let i = 0; i < n; i++) {
    const s = vals[i];
    if (!s || !s.trim()) { cls[i] = 2; continue; }
    const v = toNum(s);
    if (v !== null) sec[i] = v; else { cls[i] = 1; textIdx.push(i); }
  }
  if (textIdx.length) {
    const low = textIdx.map((i) => vals[i].toLowerCase());
    const ord = Array.from(low.keys()).sort((a, b) => (low[a] < low[b] ? -1 : low[a] > low[b] ? 1 : a - b));
    for (let r = 0; r < ord.length; r++) sec[textIdx[ord[r]]] = r;
  }
  const perm = new Uint32Array(n);
  for (let i = 0; i < n; i++) perm[i] = i;
  const dir = asc ? 1 : -1;
  perm.sort((a, b) => {
    const ca = cls[a], cb = cls[b];
    if (ca !== cb) {
      if (ca === 2) return 1;
      if (cb === 2) return -1;
      return asc ? ca - cb : cb - ca;
    }
    if (ca === 2) return a - b;
    const d = sec[a] - sec[b];
    return d !== 0 ? d * dir : a - b;
  });
  const out = new Int32Array(n);
  for (let i = 0; i < n; i++) out[i] = keys[perm[i]];
  return out;
}

function filteredOrder() {
  if (!D.filterOn || !D.matchBits) return D.order;
  return D.order.filter((k) => isMatchKey(k));
}

async function computeView(token, progress) {
  let keys = filteredOrder();
  if (D.sortCol !== null && D.sortCol < D.ncols && keys.length) {
    const s = await sortedKeys(keys, D.sortCol, D.sortAsc, token, progress);
    if (!s) return false;
    keys = s;
  }
  D.view = keys;
  recomputeMatchPositions();
  D.epoch++; D.struct++; D.rowCache.clear();
  return true;
}

async function opSort(col, asc) {
  const token = ++opToken;
  const prev = [D.sortCol, D.sortAsc];
  D.sortCol = col; D.sortAsc = asc;
  const ok = await computeView(token, (f) => post({ event: "progress", op: "sort", frac: f }));
  if (!ok) { [D.sortCol, D.sortAsc] = prev; return { cancelled: true }; }
  return { cancelled: false };
}

function makeSortPermanent() {
  if (D.view === D.order) return;
  const before = snap();
  if (D.filterOn) { D.filterOn = false; }
  // keep only rows currently in view if unfiltered; with a filter the order of the shown rows replaces theirs
  const inView = new Set(D.view);
  const rest = D.order.filter((k) => !inView.has(k));
  if (rest.length) { const o = new Int32Array(D.view.length + rest.length); o.set(D.view); o.set(rest, D.view.length); D.order = o; }
  else D.order = D.view;
  D.view = D.order; D.sortCol = null;
  structCmd("Sort Permanently", before, snap(), snapBytes(before));
  changed(true);
}

// ---------------------------------------------------------------------------
// search
// ---------------------------------------------------------------------------
function isMatchKey(k) {
  if (D.edits.has(k)) return D.matchNew.has(k);
  return k < D.nbase && !!(D.matchBits && D.matchBits[k]);
}

function recomputeMatchPositions() {
  if (!D.search || !D.matchBits || !D.view) { D.matchVpos = new Int32Array(0); return; }
  const out = [];
  const v = D.view;
  for (let p = 0; p < v.length; p++) if (isMatchKey(v[p])) out.push(p);
  D.matchVpos = Int32Array.from(out);
}

async function opSearch(text) {
  const token = ++opToken;
  D.search = text;
  if (!text) { D.matchBits = null; D.matchNew = new Set(); D.matchVpos = new Int32Array(0); if (D.filterOn) { D.filterOn = false; await computeView(token); } return { count: 0, vpos: D.matchVpos }; }
  const needle = text.toLowerCase();
  const bits = new Uint8Array(D.nbase);
  const B = 20000;
  const ident = isIdentityCols();
  const lo = D.voff;
  for (let i = lo, blocks = 0; i < D.nbase; i += B) {
    if (token !== opToken) return { cancelled: true };
    const j = Math.min(D.nbase, i + B);
    const raw = D.decoder.decode(D.data.subarray(D.starts[i], D.starts[j]));
    if (raw.toLowerCase().indexOf(needle) >= 0) {
      const rows = parseRecords(i, j);
      for (let m = 0; m < rows.length; m++) {
        if (D.edits.has(i + m)) continue;
        const r = ident ? rows[m] : mapCols(rows[m]);
        for (let c = 0; c < r.length; c++) if (r[c].toLowerCase().indexOf(needle) >= 0) { bits[i + m] = 1; break; }
      }
    }
    if (++blocks % 6 === 0) { post({ event: "progress", op: "search", frac: j / D.nbase }); await yieldNow(); }
  }
  D.matchBits = bits;
  D.matchNew = new Set();
  for (const [k, r] of D.edits) if (r.some((c) => c.toLowerCase().indexOf(needle) >= 0)) D.matchNew.add(k);
  if (D.filterOn) { if (!(await computeView(token))) return { cancelled: true }; }
  else recomputeMatchPositions();
  D.epoch++;
  return { count: D.matchVpos.length, vpos: D.matchVpos };
}

async function opSetFilter(on) {
  const token = ++opToken;
  D.filterOn = !!on && !!D.matchBits;
  const ok = await computeView(token, (f) => post({ event: "progress", op: "sort", frac: f }));
  return { cancelled: !ok };
}

// ---------------------------------------------------------------------------
// selection helpers (copy, stats), measuring
// ---------------------------------------------------------------------------
function tsvField(s) { return /[\t\n\r"]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; }

function selectionText(r0, c0, r1, c1) {
  const out = [];
  const step = 5000;
  for (let r = r0; r <= r1; r += step) {
    const rows = viewRows(r, Math.min(step, r1 - r + 1));
    for (const row of rows) {
      const cells = [];
      for (let c = c0; c <= c1; c++) cells.push(tsvField(row[c] ?? ""));
      out.push(cells.join("\t"));
    }
  }
  return out.join("\n");
}

function selectionStats(r0, c0, r1, c1) {
  const cnt = (r1 - r0 + 1) * (c1 - c0 + 1);
  if (cnt > 40000) return null;
  let n = 0, sum = 0, min = Infinity, max = -Infinity;
  for (const row of viewRows(r0, r1 - r0 + 1)) {
    for (let c = c0; c <= c1; c++) {
      const v = toNum(row[c] ?? "");
      if (v !== null) { n++; sum += v; if (v < min) min = v; if (v > max) max = v; }
    }
  }
  return n ? { n, sum, min, max } : null;
}

function sampleKeys() {
  const n = D.nbase;
  const keys = new Set();
  for (let i = D.voff; i < Math.min(n, 2500); i++) keys.add(i);
  for (let i = Math.max(D.voff, n - 400); i < n; i++) keys.add(i);
  if (n > 3000) for (let i = D.voff; i < n; i += Math.max(1, Math.floor(n / 600))) keys.add(i);
  return [...keys].sort((a, b) => a - b);
}

function measure() {
  const rows = rowsOfKeys(sampleKeys().slice(0, 4000));
  const widths = new Array(D.ncols).fill(0);
  const types = [];
  for (let c = 0; c < D.ncols; c++) {
    const vals = [];
    for (let r = 0; r < rows.length; r++) {
      const s = rows[r][c] ?? "";
      vals.push(s);
      if (r < 300 && s.length > widths[c]) widths[c] = s.length;
    }
    types.push(inferType(vals));
  }
  return { widths, types };
}

async function opInfo() {
  const m = measure();
  const empties = new Array(D.ncols).fill(0);
  let counted = 0;
  const total = viewLen();
  if (total <= 2000000) {
    for (let r = 0; r < D.order.length; r += 20000) {
      const rows = rowsOfKeys(Array.from(D.order.subarray(r, Math.min(D.order.length, r + 20000))));
      for (const row of rows) { for (let c = 0; c < D.ncols; c++) if (!row[c] || !row[c].trim()) empties[c]++; counted++; }
      await yieldNow();
    }
  }
  return { types: m.types, empties: counted ? empties : null, rows: D.order.length };
}

// ---------------------------------------------------------------------------
// encoding for save
// ---------------------------------------------------------------------------
const encoders = new Map();
function makeEncoder(label) {
  const l = label.toLowerCase();
  if (l === "utf-8") { const te = new TextEncoder(); return (s) => te.encode(s); }
  if (l === "utf-16le" || l === "utf-16be") {
    const le = l === "utf-16le";
    return (s) => {
      const out = new Uint8Array(s.length * 2);
      for (let i = 0; i < s.length; i++) {
        const c = s.charCodeAt(i);
        out[i * 2 + (le ? 0 : 1)] = c & 255;
        out[i * 2 + (le ? 1 : 0)] = c >> 8;
      }
      return out;
    };
  }
  let map = encoders.get(l);
  if (!map) {
    map = new Map();
    const dec = new TextDecoder(l);
    for (let b = 0; b < 256; b++) { const ch = dec.decode(new Uint8Array([b])); if (ch.length === 1 && ch !== "�") if (!map.has(ch)) map.set(ch, [b]); }
    if (!["windows-1252", "iso-8859-1", "latin1"].includes(l)) {
      const two = new Uint8Array(2);
      for (let a = 0x81; a <= 0xFE; a++) for (let t = 0x40; t <= 0xFE; t++) {
        two[0] = a; two[1] = t;
        const ch = dec.decode(two);
        if ([...ch].length === 1 && ch !== "�" && ch.charCodeAt(0) > 127 && !map.has(ch)) map.set(ch, [a, t]);
      }
    }
    encoders.set(l, map);
  }
  return (s) => {
    const out = [];
    for (const ch of s) {
      const cp = ch.codePointAt(0);
      if (cp < 128) { out.push(cp); continue; }
      const m = map.get(ch);
      if (!m) throw new Error("unencodable");
      for (const b of m) out.push(b);
    }
    return Uint8Array.from(out);
  };
}

// ---------------------------------------------------------------------------
// serialization
// ---------------------------------------------------------------------------
function unparse(rows, delim, quoting, eol) {
  return Papa.unparse(rows, { delimiter: delim, newline: eol, quoteChar: D.quote, escapeChar: D.quote, quotes: quoting === "all", skipEmptyLines: false });
}

function typedCell(types, c, s) {
  if (!s) return null;
  if (types[c] === "number") {
    const v = toNum(s);
    if (v !== null && !/^[+-]?0\d/.test(s)) return v;
  }
  return s;
}

/** Async iterator of row batches in document order (header excluded). */
async function* rowBatches(batch = 20000) {
  for (let i = 0; i < D.order.length; i += batch) {
    yield rowsOfKeys(Array.from(D.order.subarray(i, Math.min(D.order.length, i + batch))));
    await yieldNow();
  }
}

const pristine = () => D.undo.length === 0;

async function opSerialize(o) {
  // o: { fmt: "csv"|"tsv"|"json", forceUtf8? }
  const fmt = o.fmt || "csv";
  if (fmt === "json") return serializeJson();
  const delim = fmt === "tsv" ? "\t" : (o.delim || D.delim);
  if (fmt === "csv" && pristine() && !o.delim && D.outEol === D.eol && D.outQuoting === D.quoting) {
    return { blob: D.blob, note: "", fmt };
  }
  let outEnc = o.forceUtf8 ? "utf-8" : D.outEnc;
  try {
    return await serializeDelimited(delim, outEnc, !!o.forceUtf8);
  } catch (e) {
    if (e && e.message === "unencodable" && outEnc !== "utf-8") {
      const r = await serializeDelimited(delim, "utf-8", true);
      r.note = "Some characters don’t exist in " + encLabel(D.outEnc) + "; the file was saved as UTF-8.";
      return r;
    }
    throw e;
  }
}

async function serializeDelimited(delim, outEnc, utf8Fallback) {
  const enc = makeEncoder(outEnc);
  const eol = D.outEol;
  const eolBytes = enc(eol);
  const quoting = D.outQuoting;
  const sameEnc = outEnc === D.outEnc && !D.transcoded;
  const rawOk = sameEnc && isIdentityCols() && delim === D.delim && quoting === D.quoting && eol === D.eol;
  const bom = utf8Fallback ? new Uint8Array(0) : D.bom;
  const parts = [];
  if (bom.length) parts.push(bom);
  const total = D.order.length + (D.hasHeader ? 1 : 0);
  const textRows = [];
  let run = null; // [k0, k1) pending raw run
  let needEol = false;
  const flushText = () => {
    if (!textRows.length) return;
    if (needEol) { parts.push(eolBytes); needEol = false; }
    parts.push(enc(unparse(textRows, delim, quoting, eol) + eol));
    textRows.length = 0;
  };
  const flushRun = () => {
    if (!run) return;
    const [k0, k1] = run;
    run = null;
    if (needEol) { parts.push(eolBytes); needEol = false; }
    const a = D.starts[k0], b = D.starts[k1];
    parts.push(D.blob.slice(a, b));
    if (k1 === D.nbase && !D.endsWithNewline) needEol = true; // last record has no terminator
  };
  const emitRaw = (k) => {
    if (textRows.length) flushText();
    if (run && run[1] === k) run[1] = k + 1; else { flushRun(); run = [k, k + 1]; }
  };
  const emitRow = (row) => { flushRun(); textRows.push(row); };

  let seen = 0;
  const progress = () => post({ event: "progress", op: "save", frac: seen / Math.max(1, total) });
  if (D.hasHeader) {
    if (rawOk && headerIsRaw()) emitRaw(D.headerKey); else emitRow(D.headers.slice());
    seen++;
  }
  if (rawOk) {
    const ord = D.order;
    for (let i = 0; i < ord.length; i++) {
      const k = ord[i];
      if (k >= D.nbase || D.edits.has(k)) emitRow(rowsOfKeys([k])[0]);
      else emitRaw(k);
      if ((i & 0x3ffff) === 0x3ffff) { seen = i; progress(); await yieldNow(); }
      if (textRows.length >= 20000) flushText();
    }
  } else {
    for await (const rows of rowBatches()) {
      for (const r of rows) emitRow(r);
      flushText();
      seen += rows.length; progress();
    }
  }
  flushRun(); flushText();
  // A file that had no trailing newline keeps none (only when the last emitted piece ends the document).
  if (!D.endsWithNewline && !needEol && parts.length && !(rawOk && lastWasRaw(parts))) {
    // last piece is serialized text ending with eol: drop that eol
    const last = parts[parts.length - 1];
    if (last instanceof Uint8Array && last.length >= eolBytes.length) parts[parts.length - 1] = last.subarray(0, last.length - eolBytes.length);
  }
  return { blob: new Blob(parts, { type: "text/csv" }), note: "", fmt: delim === "\t" ? "tsv" : "csv" };
}

function lastWasRaw(parts) { return parts[parts.length - 1] instanceof Blob; }

async function serializeJson() {
  const types = measure().types;
  const names = [];
  const seen = new Map();
  for (let c = 0; c < D.ncols; c++) {
    let nm = D.hasHeader && D.headers[c] ? D.headers[c] : `col${c + 1}`;
    if (seen.has(nm)) { const k = seen.get(nm) + 1; seen.set(nm, k); nm = `${nm}_${k}`; } else seen.set(nm, 1);
    names.push(nm);
  }
  const parts = ["[\n"];
  let first = true;
  for await (const rows of rowBatches()) {
    const chunk = [];
    for (const r of rows) {
      const obj = D.hasHeader ? {} : [];
      for (let c = 0; c < D.ncols; c++) {
        const v = typedCell(types, c, r[c] ?? "");
        if (D.hasHeader) obj[names[c]] = v; else obj.push(v);
      }
      chunk.push("  " + JSON.stringify(obj));
    }
    if (chunk.length) { parts.push((first ? "" : ",\n") + chunk.join(",\n")); first = false; }
  }
  parts.push("\n]\n");
  return { blob: new Blob(parts, { type: "application/json" }), note: "", fmt: "json" };
}

// ---------------------------------------------------------------------------
// ops
// ---------------------------------------------------------------------------
async function opLoad({ blob, name, encoding, delimiter, header }) {
  const token = (D ? D.token : 0) + 1;
  D = newDoc();
  D.token = token; opToken++;
  D.blob = blob; D.name = name || "";
  D.fileSize = blob.size;
  D.data = new Uint8Array(await blob.arrayBuffer());
  detectEncoding(encoding);
  D.decoder = new TextDecoder(D.enc, { ignoreBOM: true });
  D.starts = new Uint32Array(1); D.starts[0] = D.baseOff;
  detectDialect({ delimiter, header });
  return new Promise((resolve, reject) => {
    scanAll(token, () => resolve(stateOf())).then(() => resolve(stateOf()), (e) => {
      reject(e);
      post({ event: "error", message: String((e && e.message) || e) });
    });
  });
}

const ops = {
  load: opLoad,
  state: () => stateOf(),
  rows: ({ start, count }) => ({ epoch: D.epoch, struct: D.struct, rows: viewRows(start, count) }),
  setCells: ({ changes, label }) => { setCells(changes, label); return stateOf(); },
  insertRows: ({ vpos, count }) => { insertRows(vpos, count); return stateOf(); },
  deleteRows: ({ ranges }) => { deleteRows(ranges); return stateOf(); },
  insertCols: ({ at, count }) => { insertCols(at, count); return stateOf(); },
  deleteCols: ({ cols }) => { const ok = deleteCols(cols); return { ok, state: stateOf() }; },
  setHeader: ({ col, text }) => { setHeader(col, text); return stateOf(); },
  setHasHeader: ({ flag }) => { setHasHeader(flag); return stateOf(); },
  paste: ({ r0, c0, grid }) => {
    withGroup("Paste", () => {
      const needRows = r0 + grid.length - viewLen();
      if (needRows > 0) insertRows(viewLen(), needRows);
      const width = grid.reduce((m, r) => Math.max(m, r.length), 0);
      const needCols = c0 + width - D.ncols;
      if (needCols > 0) insertCols(D.ncols, needCols);
      const changes = [];
      grid.forEach((row, i) => row.forEach((t, j) => changes.push([r0 + i, c0 + j, t])));
      setCells(changes, "Paste");
    });
    return stateOf();
  },
  clear: ({ r0, c0, r1, c1 }) => {
    const ch = [];
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) ch.push([r, c, ""]);
    withGroup("Clear Cells", () => setCells(ch, "Clear Cells"));
    return stateOf();
  },
  undo: () => { doUndo(); return stateOf(); },
  redo: () => { doRedo(); return stateOf(); },
  sort: ({ col, asc }) => opSort(col, asc).then((r) => ({ ...r, state: stateOf() })),
  clearSort: async () => {
    const token = ++opToken;
    D.sortCol = null;
    await computeView(token);
    return { state: stateOf() };
  },
  sortPermanent: () => { makeSortPermanent(); return stateOf(); },
  search: async ({ text }) => {
    const r = await opSearch(text);
    if (r.cancelled) return { cancelled: true };
    return { count: r.count, vpos: r.vpos, state: stateOf() };
  },
  filter: async ({ on }) => { const r = await opSetFilter(on); return { ...r, vpos: D.matchVpos, state: stateOf() }; },
  selectionText: ({ r0, c0, r1, c1 }) => selectionText(r0, c0, r1, c1),
  selectionStats: ({ r0, c0, r1, c1 }) => selectionStats(r0, c0, r1, c1),
  measure: () => measure(),
  info: () => opInfo(),
  setOutput: ({ eol, quoting }) => {
    const before = snap();
    if (eol) D.outEol = eol;
    if (quoting) D.outQuoting = quoting;
    structCmd("Change Output Format", before, snap(), 64);
    changed(false);
    return stateOf();
  },
  // markSaved({pending: true}) marks the state captured by the last serialize() as saved —
  // the shell confirms the write only after the save dialog, and the user may have
  // edited meanwhile (Viewer.onSaved). Without `pending`, the current state.
  markSaved: (o) => {
    D.savedMarker = o && o.pending ? D.pendingMarker : D.undo.length ? D.undo[D.undo.length - 1] : null;
    return stateOf();
  },
  serialize: (o) => {
    const marker = D.undo.length ? D.undo[D.undo.length - 1] : null;
    return opSerialize(o).then((r) => { D.pendingMarker = marker; return r; });
  },
};

self.onmessage = async (ev) => {
  const { id, op, ...args } = ev.data;
  try {
    const fn = ops[op];
    if (!fn) throw new Error("unknown op " + op);
    const result = await fn(args);
    let transfer = [];
    if (result && result.vpos instanceof Int32Array) transfer = [result.vpos.buffer];
    post({ id, ok: true, result }, transfer);
  } catch (e) {
    post({ id, ok: false, error: String((e && e.message) || e) });
  }
};

self.ViewTable = { ops, toNum, inferType, get doc() { return D; } };
