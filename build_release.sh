#!/bin/zsh
# Builds the self-contained, downloadable app: dist/MiBand-AirMouse-<version>-<arch>.zip
# It bundles a relocatable CPython (astral-sh/python-build-standalone), the libraries from
# requirements.txt and this code, so users need neither Xcode, Python nor ~/airmouse.
#   ./build_release.sh            # arm64 (Apple Silicon)
#   ./build_release.sh x86_64     # Intel Macs (pip runs under Rosetta)
set -e
setopt null_glob
cd "$(dirname "$0")"
ARCH="${1:-arm64}"
VERSION="${VERSION:-1.1.0}"
PY_RELEASE=20261021
PY_VERSION=3.12.15
case "$ARCH" in
  arm64) PY_ARCH=aarch64; RUN=() ;;
  x86_64) PY_ARCH=x86_64; RUN=(arch -x86_64) ;;
  *) echo "unknown arch: $ARCH"; exit 1 ;;
esac

TARBALL="cpython-$PY_VERSION+$PY_RELEASE-$PY_ARCH-apple-darwin-install_only_stripped.tar.gz"
BASE="https://github.com/astral-sh/python-build-standalone/releases/download/$PY_RELEASE"
CACHE=build/cache
mkdir -p "$CACHE"
if [ ! -f "$CACHE/$TARBALL" ]; then
  curl -fsSL --retry 3 -o "$CACHE/$TARBALL.part" "$BASE/$TARBALL"
  mv "$CACHE/$TARBALL.part" "$CACHE/$TARBALL"
fi
[ -f "$CACHE/SHA256SUMS-$PY_RELEASE" ] || curl -fsSL -o "$CACHE/SHA256SUMS-$PY_RELEASE" "$BASE/SHA256SUMS"
want=$(grep " $TARBALL\$" "$CACHE/SHA256SUMS-$PY_RELEASE" | cut -d' ' -f1)
have=$(shasum -a 256 "$CACHE/$TARBALL" | cut -d' ' -f1)
[ -n "$want" ] && [ "$want" = "$have" ] || { echo "checksum mismatch for $TARBALL"; exit 1; }

OUT="dist/$ARCH"
APP="$OUT/MiBand AirMouse.app"
rm -rf "$OUT"
mkdir -p "$OUT"
APP="$APP" ARCH="$ARCH" VERSION="$VERSION" SIGN=0 ./build_app.sh
RES="$APP/Contents/Resources"

# Python runtime, without the parts the app never uses
tar -xzf "$CACHE/$TARBALL" -C "$RES"
mv "$RES/python" "$RES/runtime"
LIB="$RES/runtime/lib/python3.12"
rm -rf "$RES/runtime/include" "$RES/runtime/share" "$RES"/runtime/lib/{tcl,tk,itcl,thread}* \
  "$LIB"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,pydoc_data} "$LIB"/turtle.py
"${RUN[@]}" "$RES/runtime/bin/python3" -m pip install -q --no-cache-dir --disable-pip-version-check \
  -r requirements.txt
"${RUN[@]}" "$RES/runtime/bin/python3" -m pip uninstall -q -y pip
rm -f "$RES"/runtime/bin/pip*

# The app's own code
mkdir -p "$RES/app"
cp *.py "$RES/app/"
cp -R xproto "$RES/app/xproto"
rm -f "$RES/app/make_icon.py"
find "$RES" -name __pycache__ -prune -exec rm -rf {} +

# Precompile now: the signed bundle must never be written to at run time
"${RUN[@]}" "$RES/runtime/bin/python3" -m compileall -q -j 0 "$RES/runtime/lib" "$RES/app" >/dev/null

codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
ZIP="dist/MiBand-AirMouse-$VERSION-$ARCH.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
rm -rf "$OUT"  # only the zip is kept: a stray .app would show up in Spotlight/Launchpad
echo "Built: $ZIP ($(du -h "$ZIP" | cut -f1))"
