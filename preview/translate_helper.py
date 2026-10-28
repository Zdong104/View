"""Translation helper process (view-translate.exe on Windows).

Runs ctranslate2 for an app whose own Python cannot load it (the Windows
build runs on MSYS2's Python; ctranslate2 only ships for python.org's), like
preview.ocr_helper.  Speaks the protocol of ``preview.translate._Helper`` on
stdin/stdout, one JSON object per line:

    -> {"ready": true, "version": "..."}                 once, at start
    <- {"text": "...", "dirs": [pack dir, ...], "tgt": "es"}
    -> {"partial": "..."} ... then {"text": "..."}       or {"error": "..."}

The app downloads the language packs itself; the helper only reads them.

Self-test:  view-translate.exe --test PACK_DIR TEXT   (prints the translation)
"""
from __future__ import annotations

import json
import os
import sys

from . import __version__, translate


def _send(out, obj):
    out.write(json.dumps(obj).encode() + b"\n")
    out.flush()


def serve(inp, out):
    try:          # fail at once, with the reason, if the engine cannot load
        import ctranslate2
        import sentencepiece  # noqa: F401
    except Exception as e:  # noqa: BLE001
        _send(out, {"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    _send(out, {"ready": True, "version": __version__, "ctranslate2": ctranslate2.__version__})
    while True:
        line = inp.readline()
        if not line:
            return 0
        try:
            req = json.loads(line)
            text, dirs, tgt = str(req["text"]), [str(d) for d in req["dirs"]], str(req["tgt"])
        except Exception as e:  # noqa: BLE001  (a malformed request: report, keep going)
            _send(out, {"error": f"bad request: {e}"})
            continue
        try:
            res = translate.translate_local(text, dirs, tgt,
                                            lambda t: _send(out, {"partial": t}))
            _send(out, {"text": res})
        except Exception as e:  # noqa: BLE001
            _send(out, {"error": f"{type(e).__name__}: {e}"})


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    translate._LOCAL_ONLY = True
    if argv[:1] in (["--version"], ["-V"]):
        import ctranslate2
        print(f"view-translate {__version__} (ctranslate2 {ctranslate2.__version__})")
        return 0
    if argv[:1] == ["--test"] and len(argv) == 3:
        d = argv[1]
        tgt = os.path.basename(os.path.normpath(d)).split("_", 1)[-1]
        print(translate.translate_local(argv[2], [d], tgt))
        return 0
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    out = sys.stdout.buffer
    sys.stdout = sys.stderr       # stdout carries the protocol
    return serve(sys.stdin.buffer, out)


if __name__ == "__main__":
    sys.exit(main())
