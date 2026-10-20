// Read ModelProto.metadata_props (field 14) from an ONNX file without a
// protobuf library. onnxruntime-web does not expose custom metadata, and the
// PP-OCR recognizer keeps its dictionary there under "character".

function varint(b: Uint8Array, pos: number): [number, number] {
  let v = 0, mul = 1;
  for (;;) {
    const byte = b[pos++];
    if (byte === undefined) throw new Error("truncated ONNX file");
    v += (byte & 0x7f) * mul;
    if (byte < 0x80) return [v, pos];
    mul *= 128;
  }
}

/** Walk the fields of one message: calls fn(field, wireType, start, end) for each. */
function fields(b: Uint8Array, start: number, end: number, fn: (field: number, start: number, end: number) => void) {
  let p = start;
  while (p < end) {
    let tag: number;
    [tag, p] = varint(b, p);
    const field = Math.floor(tag / 8), wt = tag & 7;
    if (wt === 0) [, p] = varint(b, p);
    else if (wt === 1) p += 8;
    else if (wt === 5) p += 4;
    else if (wt === 2) {
      let len: number;
      [len, p] = varint(b, p);
      fn(field, p, p + len);
      p += len;
    } else throw new Error(`unsupported protobuf wire type ${wt}`);
  }
}

export function onnxMetadata(model: Uint8Array): Record<string, string> {
  const out: Record<string, string> = {};
  const dec = new TextDecoder();
  fields(model, 0, model.length, (f, s, e) => {
    if (f !== 14) return;
    let key = "", value = "";
    fields(model, s, e, (g, s2, e2) => {
      if (g === 1) key = dec.decode(model.subarray(s2, e2));
      else if (g === 2) value = dec.decode(model.subarray(s2, e2));
    });
    out[key] = value;
  });
  return out;
}
