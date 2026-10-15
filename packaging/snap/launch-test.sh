#!/usr/bin/env bash
# Launch the installed snap the way a user does (snap run view FILE, a real
# session bus, a display) and check that it stays up, owns its D-Bus name and
# shows the PDF in a window.  --selftest can't catch launch problems: it runs
# under its own non-unique application ID.
#
#   packaging/snap/launch-test.sh            (re-executes itself under
#                                             dbus-run-session + xvfb-run)
# Needs: xvfb, xdotool, dbus (dbus-run-session), libglib2.0-bin (gdbus).
set -euo pipefail
APP_ID=io.github.zdong104.View
OUT="${OUT:-$PWD/launch-test}"

if [ -z "${VIEW_LAUNCH_TEST_INNER:-}" ]; then
    mkdir -p "$OUT"
    export VIEW_LAUNCH_TEST_INNER=1 OUT
    exec dbus-run-session -- xvfb-run -a -s "-screen 0 1280x800x24" "$0" "$@"
fi

# A one-page PDF in $HOME (the snap's home plug can't read /tmp or dot-dirs).
PDF="$HOME/view-launch-test.pdf"
python3 - "$PDF" <<'PY'
import sys
objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
text = b"BT /F1 24 Tf 40 100 Td (Launch test) Tj ET"
objs[3] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text)
out = bytearray(b"%PDF-1.4\n")
offs = []
for i, o in enumerate(objs, 1):
    offs.append(len(out))
    out += b"%d 0 obj\n%s\nendobj\n" % (i, o)
xref = len(out)
out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
out += b"".join(b"%010d 00000 n \n" % o for o in offs)
out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
open(sys.argv[1], "wb").write(out)
PY

export GSK_RENDERER=cairo           # Xvfb has no GPU
snap run view "$PDF" >"$OUT/view.log" 2>&1 &
pid=$!
status=1
msg="no window for the PDF after 90 s"
for _ in $(seq 90); do
    if ! kill -0 "$pid" 2>/dev/null; then
        msg="View exited right after launch"
        break
    fi
    if xdotool search --name "view-launch-test.pdf" >/dev/null 2>&1; then
        owned=$(gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
                --method org.freedesktop.DBus.NameHasOwner "$APP_ID")
        if [ "$owned" = "(true,)" ]; then
            msg="window shows the PDF, $APP_ID is on the session bus"
            status=0
        else
            msg="window is up but $APP_ID is not on the session bus: $owned"
        fi
        break
    fi
    sleep 1
done
import -window root "$OUT/screenshot.png" 2>/dev/null || true
kill "$pid" 2>/dev/null || true
wait "$pid" 2>/dev/null || true
rm -f "$PDF"
echo "--- view.log"; cat "$OUT/view.log"
echo "--- AppArmor denials"; sudo dmesg 2>/dev/null | grep -i 'apparmor="DENIED".*snap.view' | tail -20 || true
if [ "$status" = 0 ]; then echo "PASSED: $msg"; else echo "FAILED: $msg"; fi
exit "$status"
