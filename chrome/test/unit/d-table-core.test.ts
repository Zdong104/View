// The table worker's document model (src/viewers/table/worker.js), run in a vm with the
// worker globals stubbed: detection, row parsing, edits, undo, sort, search, and
// byte-exact saving.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import vm from "node:vm";
import Papa from "papaparse";

type Msg = { id?: number; ok?: boolean; result?: unknown; error?: string };

function makeWorker() {
  const msgs: Msg[] = [];
  const self: Record<string, unknown> = { postMessage: (m: Msg) => msgs.push(m) };
  const ctx = vm.createContext({
    self, Papa, importScripts() {}, TextDecoder, TextEncoder, Blob, setTimeout, Uint8Array, Int32Array, Uint32Array,
    Float64Array, Map, Set, Array, JSON, Math, Promise, Object, Number, String, RegExp, Error,
  });
  vm.runInContext(readFileSync(resolve(process.cwd(), "src/viewers/table/worker.js"), "utf8"), ctx);
  let id = 0;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const call = <T = any>(op: string, args: Record<string, unknown> = {}) => new Promise<T>((res, rej) => {
    const myid = ++id;
    const iv = setInterval(() => {
      const i = msgs.findIndex((m) => m.id === myid);
      if (i < 0) return;
      clearInterval(iv);
      const m = msgs.splice(i, 1)[0]!;
      if (m.ok) res(m.result as T); else rej(new Error(m.error));
    }, 1);
    (self.onmessage as (e: unknown) => void)({ data: { id: myid, op, ...args } });
  });
  return call;
}

const bytes = async (b: Blob) => Buffer.from(await b.arrayBuffer());

async function load(call: ReturnType<typeof makeWorker>, buf: Buffer, name = "x.csv", extra: Record<string, unknown> = {}) {
  await call("load", { blob: new Blob([buf as BlobPart]), name, ...extra });
  for (let i = 0; i < 400 && !(await call("state")).loaded; i++) await new Promise((r) => setTimeout(r, 5));
  return call("state");
}

const QUIRKY = Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]),
  Buffer.from('name,value,note\r\n"a, b",1,"he said ""hi"""\r\nplain,2,"multi\r\nline"\r\n中文,3,x\r\n', "utf8")]);

test("detects BOM, CRLF, header and parses quoted multi-line fields", async () => {
  const call = makeWorker();
  const st = await load(call, QUIRKY);
  assert.equal(st.nView, 3);
  assert.equal(st.ncols, 3);
  assert.equal(st.hasHeader, true);
  assert.equal(st.eol, "\r\n");
  assert.equal(st.encLabel, "UTF-8 (BOM)");
  assert.deepEqual(st.headers, ["name", "value", "note"]);
  const r = await call("rows", { start: 0, count: 10 });
  assert.deepEqual(r.rows, [["a, b", "1", 'he said "hi"'], ["plain", "2", "multi\r\nline"], ["中文", "3", "x"]]);
});

test("untouched, edited and undone documents save byte-exactly", async () => {
  const call = makeWorker();
  await load(call, QUIRKY);
  assert.ok((await bytes((await call("serialize", { fmt: "csv" })).blob)).equals(QUIRKY));
  await call("setCells", { changes: [[1, 1, "22"]] });
  const edited = (await bytes((await call("serialize", { fmt: "csv" })).blob)).toString("utf8");
  assert.equal(edited, QUIRKY.toString("utf8").replace("plain,2,", "plain,22,"));
  const st = await call("undo");
  assert.equal(st.dirty, false);
  assert.ok((await bytes((await call("serialize", { fmt: "csv" })).blob)).equals(QUIRKY));
});

test("no trailing newline is preserved", async () => {
  const call = makeWorker();
  const src = Buffer.from("a;b\n1;2\n3;4");
  const st = await load(call, src, "n.csv");
  assert.equal(st.delim, ";");
  assert.ok((await bytes((await call("serialize", { fmt: "csv" })).blob)).equals(src));
  await call("setCells", { changes: [[1, 0, "33"]] });
  assert.equal((await bytes((await call("serialize", { fmt: "csv" })).blob)).toString(), "a;b\n1;2\n33;4");
});

test("rows, columns, header toggle, and undo back to the original", async () => {
  const call = makeWorker();
  const src = Buffer.from("a,b\n1,2\n3,4\n");
  await load(call, src);
  await call("insertRows", { vpos: 1, count: 1 });
  await call("setCells", { changes: [[1, 0, "new,row"]] });
  await call("insertCols", { at: 2, count: 1 });
  assert.equal((await bytes((await call("serialize", { fmt: "csv" })).blob)).toString(), 'a,b,\n1,2,\n"new,row",,\n3,4,\n');
  await call("deleteCols", { cols: [0] });
  assert.equal((await bytes((await call("serialize", { fmt: "csv" })).blob)).toString(), 'b,\n2,\n,\n4,\n');
  for (let i = 0; i < 6; i++) await call("undo");
  assert.ok((await bytes((await call("serialize", { fmt: "csv" })).blob)).equals(src));
  let st = await call("setHasHeader", { flag: false });
  assert.equal(st.nView, 3);
  assert.ok((await bytes((await call("serialize", { fmt: "csv" })).blob)).equals(src));
  st = await call("setHasHeader", { flag: true });
  assert.equal(st.nView, 2);
});

test("legacy and UTF-16 encodings round-trip", async () => {
  const call = makeWorker();
  const latin = Buffer.from("name,city\nJosé,Zürich\n", "latin1");
  const st = await load(call, latin);
  assert.equal(st.encLabel, "Windows-1252");
  await call("setCells", { changes: [[0, 1, "Köln"]] });
  assert.ok((await bytes((await call("serialize", { fmt: "csv" })).blob)).equals(Buffer.from("name,city\nJosé,Köln\n", "latin1")));
  const u16 = Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from("a,b\n1,2\n", "utf16le")]);
  const st2 = await load(call, u16);
  assert.equal(st2.encLabel, "UTF-16 LE (BOM)");
  assert.ok((await bytes((await call("serialize", { fmt: "csv" })).blob)).equals(u16));
  await call("setCells", { changes: [[0, 0, "9"]] });
  assert.equal((await bytes((await call("serialize", { fmt: "csv" })).blob)).toString("hex"), "fffe61002c0062000a0039002c0032000a00");
});

test("sort puts numbers before text and empties last, search finds cells, filter narrows", async () => {
  const call = makeWorker();
  await load(call, Buffer.from("name,score\nbob,10\nann,9\ncat,100\ndan,\neve,abc\n"));
  await call("sort", { col: 1, asc: true });
  assert.deepEqual((await call("rows", { start: 0, count: 5 })).rows.map((r: string[]) => r[1]), ["9", "10", "100", "abc", ""]);
  await call("sort", { col: 1, asc: false });
  assert.deepEqual((await call("rows", { start: 0, count: 5 })).rows.map((r: string[]) => r[1]), ["abc", "100", "10", "9", ""]);
  await call("clearSort");
  const s = await call("search", { text: "AN" });
  assert.deepEqual(Array.from(s.vpos as Int32Array), [1, 3]); // ann, dan
  const f = await call("filter", { on: true });
  assert.equal(f.state.nView, 2);
});

test("paste grows the table; JSON export types numbers", async () => {
  const call = makeWorker();
  await load(call, Buffer.from("a,b\n1,x\n"));
  const st = await call("paste", { r0: 0, c0: 1, grid: [["y", "z"], ["p", "q"]] });
  assert.equal(st.nView, 2);
  assert.equal(st.ncols, 3);
  const json = JSON.parse((await bytes((await call("serialize", { fmt: "json" })).blob)).toString());
  assert.deepEqual(json[0], { a: 1, b: "y", col3: "z" });
});
