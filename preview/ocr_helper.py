"""Text-recognition helper process (view-ocr.exe on Windows).

Runs RapidOCR for an app whose own Python cannot load onnxruntime (the
Windows build runs on MSYS2's Python; onnxruntime only ships for
python.org's).  Speaks the protocol of ``preview.ocr._Helper`` on
stdin/stdout:

    -> {"ready": true, "version": "..."}             once, at start
    <- {"cmd": "load"}                               load the models now
    <- {"w": W, "h": H} + W*H*3 bytes of RGB         recognize
    -> {"lines": [[box, text, confidence], ...]}     or {"error": "..."}

Self-test:  view-ocr.exe --test IMAGE EXPECTED  (exit 0 if EXPECTED is found)
"""
from __future__ import annotations

import json
import sys

from . import __version__, ocr


def _send(out, obj):
    out.write(json.dumps(obj).encode() + b"\n")
    out.flush()


def _read_exact(inp, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = inp.read(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return bytes(buf)


def serve(inp, out):
    import numpy as np
    _send(out, {"ready": True, "version": __version__})
    while True:
        line = inp.readline()
        if not line:
            return 0
        try:
            req = json.loads(line)
            if req.get("cmd") == "load":
                ocr._get_engine()
                _send(out, {"ok": True})
                continue
            w, h = int(req["w"]), int(req["h"])
            data = _read_exact(inp, w * h * 3)
        except EOFError:
            return 0
        except Exception as e:  # noqa: BLE001  (a malformed request: report, keep going)
            _send(out, {"error": f"bad request: {e}"})
            continue
        try:
            rgb = np.frombuffer(data, np.uint8).reshape(h, w, 3)
            lines = ocr.recognize_raw(np.ascontiguousarray(rgb[:, :, ::-1]))
            _send(out, {"lines": lines})
        except Exception as e:  # noqa: BLE001
            _send(out, {"error": f"{type(e).__name__}: {e}"})


def _test(path, expected):
    text = ocr.lines_to_text(ocr.recognize(path))
    print(text)
    if expected not in text:
        print(f"FAILED: {expected!r} not recognized", file=sys.stderr)
        return 1
    print("PASSED")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ocr._LOCAL_ONLY = True
    if argv[:1] in (["--version"], ["-V"]):
        print(f"view-ocr {__version__}")
        return 0
    if argv[:1] == ["--test"] and len(argv) == 3:
        return _test(argv[1], argv[2])
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    # stdout carries the protocol: keep stray prints (rapidocr logging) off it
    out = sys.stdout.buffer
    sys.stdout = sys.stderr
    return serve(sys.stdin.buffer, out)


if __name__ == "__main__":
    sys.exit(main())
