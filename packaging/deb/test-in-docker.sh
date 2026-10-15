#!/usr/bin/env bash
# Install the .deb into a clean container with apt (proves the Depends resolve)
# and run the self-test, headless and then with a virtual X display.
#
#   packaging/deb/test-in-docker.sh [IMAGE] [DEB]
#   IMAGE defaults to ubuntu:26.04; DEB to the newest dist/view_*_all.deb
#   LINTIAN=1 also runs lintian inside the container.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${1:-ubuntu:26.04}"
DEB="${2:-$(ls -t "$ROOT"/dist/view_*_all.deb | head -n1)}"
DEB_DIR="$(cd "$(dirname "$DEB")" && pwd)"
DEB_NAME="$(basename "$DEB")"

echo "=== $IMAGE: $DEB_NAME"
docker run --rm -e DEBIAN_FRONTEND=noninteractive -e LINTIAN="${LINTIAN:-0}" \
    -v "$DEB_DIR:/dist:ro" "$IMAGE" bash -euo pipefail -c "
apt-get update -qq
# Depends only (no Recommends): the minimum a user gets with --no-install-recommends
if ! apt-get install -y -qq --no-install-recommends /dist/$DEB_NAME >/tmp/apt.log 2>&1; then
    echo 'RESULT: apt could not install the package'
    grep -E 'Depends|unmet|E:|but' /tmp/apt.log | head -n 30
    exit 2
fi
echo \"installed: \$(dpkg-query -W -f='\${Version}' view)\"
view-app --version
echo '--- selftest (headless)'
view-app --selftest --no-ui
echo '--- selftest (Xvfb)'
apt-get install -y -qq --no-install-recommends xvfb xauth >/dev/null 2>&1
xvfb-run -a -s '-screen 0 1280x1024x24' view-app --selftest 2>&1 \
    | grep -vE 'Gtk-WARNING|^\$|dbus|AT-SPI|Fontconfig'
if [ \"\$LINTIAN\" = 1 ]; then
    echo '--- lintian'
    apt-get install -y -qq --no-install-recommends lintian >/dev/null 2>&1
    lintian --no-tag-display-limit /dist/$DEB_NAME || true
fi
echo '--- remove'
apt-get remove -y -qq view >/dev/null
test ! -e /usr/lib/view/preview/__pycache__ && echo 'clean removal'
echo RESULT: OK
"
