"""One-click update for the prebuilt app: download the new release, check it, swap it in, reopen.

The check: the new bundle must satisfy the running app's own designated requirement, i.e. be
signed by the same certificate (build_release.sh signs with "MiBand AirMouse" when that
identity is in the keychain). Only someone holding that private key can pass it, and macOS then
keeps the app's Bluetooth / Accessibility permissions. An ad-hoc signed app (requirement =
its own cdhash) can't vouch for another build, so it falls back to opening the release page.
"""
import os
import plistlib
import posixpath
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

import requests

from updates import PAGE_PREFIX

CACHE = os.path.expanduser("~/Library/Caches/MiBand AirMouse")


def bundle_path():
    """The running prebuilt .app (its Python lives in Contents/Resources/runtime), else None."""
    exe = os.path.realpath(sys.executable)
    marker = ".app/Contents/Resources/runtime/"
    if marker not in exe:
        return None
    return exe[:exe.index(marker) + len(".app")]


def designated_requirement(app):
    r = subprocess.run(["codesign", "-d", "-r-", app], capture_output=True, text=True)
    for line in (r.stdout + r.stderr).splitlines():
        line = line.removeprefix("# ")  # an ad-hoc signature's implicit "cdhash" requirement
        if line.startswith("designated => "):
            return line[len("designated => "):].strip()
    return None


def can_install(info):
    """Why a one-click install isn't possible (a short reason), or None when it is."""
    app = bundle_path()
    if not app:
        return "đang chạy từ mã nguồn"
    if not info or not info.get("download"):
        return "bản mới chưa có file cho máy này"
    req = designated_requirement(app)
    if not req or req.startswith("cdhash"):
        return "bản đang dùng chưa có chữ ký cố định"
    if not os.access(os.path.dirname(app), os.W_OK):
        return "không ghi được vào thư mục chứa app"
    return None


def prepare(info, progress, cancelled=lambda: False):
    """Download and unpack the release, check it. Returns the path of the verified new .app.
    progress(fraction or None, text) is called from this (background) thread."""
    url = info["download"]
    if not url.startswith(PAGE_PREFIX):
        raise RuntimeError("địa chỉ tải không thuộc dự án này")
    work = os.path.join(CACHE, "update")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    zip_path = os.path.join(work, "update.zip")
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        total, done = int(r.headers.get("content-length") or 0), 0
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                if cancelled():
                    raise RuntimeError("đã huỷ")
                f.write(chunk)
                done += len(chunk)
                progress(done / total if total else None, f"Đang tải {done * 100 // total}%…" if total else "Đang tải…")
    progress(None, "Đang kiểm tra…")
    check_archive(zip_path)  # before anything is written from it
    out = os.path.join(work, "new")
    subprocess.run(["ditto", "-x", "-k", zip_path, out], check=True, capture_output=True)
    apps = [n for n in os.listdir(out) if n.endswith(".app")]
    if len(apps) != 1:
        raise RuntimeError("gói tải về không đúng dạng")
    new = os.path.join(out, apps[0])
    verify(new, bundle_path(), info["version"])
    os.remove(zip_path)
    return new


def check_archive(zip_path):
    """Only files, folders and inside-pointing symlinks under one top-level .app (macOS metadata
    aside): no absolute paths, no "..", no link leading out - so unpacking can't write outside
    its folder. (The bundled Python has a few relative links, e.g. bin/python3 -> python3.12.)"""
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            parts = name.split("/")
            if name.startswith("/") or "\\" in name or ".." in parts:
                raise RuntimeError(f"gói tải về có đường dẫn lạ: {name}")
            if parts[0] != "__MACOSX" and not parts[0].endswith(".app"):
                raise RuntimeError(f"gói tải về có file ngoài app: {name}")
            if stat.S_ISLNK(info.external_attr >> 16):
                target = z.read(info).decode("utf-8", "replace")
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                if target.startswith("/") or not resolved.startswith(parts[0] + "/"):
                    raise RuntimeError(f"gói tải về có liên kết ra ngoài app: {name} -> {target}")


def verify(new, running, version):
    """The new bundle is intact, signed by the same identity as the running app, and is the
    version that was announced."""
    req = designated_requirement(running)
    if not req or req.startswith("cdhash"):
        raise RuntimeError("bản đang dùng chưa có chữ ký cố định")
    r = subprocess.run(["codesign", "--verify", "--deep", "--strict", "-R=" + req, new],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("chữ ký của bản tải về không khớp: " + (r.stderr.strip().splitlines() or ["?"])[-1])
    with open(os.path.join(new, "Contents", "Info.plist"), "rb") as f:
        found = plistlib.load(f).get("CFBundleShortVersionString")
    if found != version:
        raise RuntimeError(f"bản tải về là {found}, không phải {version}")


SWAP = r'''#!/bin/bash
# Waits for MiBand AirMouse to quit, puts the new version in its place and opens it.
# A failed swap puts the old version back.
OLD="$1"; NEW="$2"; PID="$3"; LOG="$4"; REOPEN="$5"; REQ="$6"
exec >>"$LOG" 2>&1
echo "$(date '+%H:%M:%S') swap: waiting for pid $PID"
for i in $(seq 1 150); do kill -0 "$PID" 2>/dev/null || break; sleep 0.2; done
# Checked again now: the bundle may have been changed while the app was quitting
if ! codesign --verify --deep --strict -R="$REQ" "$NEW"; then
  echo "$(date '+%H:%M:%S') swap: the new version no longer passes the signature check, not installed"
  [ "$REOPEN" = 1 ] && open "$OLD"
  exit 1
fi
BACKUP="$(dirname "$NEW")/previous.app"
rm -rf "$BACKUP"
if mv "$OLD" "$BACKUP" && mv "$NEW" "$OLD"; then
  echo "$(date '+%H:%M:%S') swap: installed $OLD"
  rm -rf "$BACKUP"
else
  echo "$(date '+%H:%M:%S') swap: failed, restoring the previous version"
  [ -d "$BACKUP" ] && [ ! -e "$OLD" ] && mv "$BACKUP" "$OLD"
fi
[ "$REOPEN" = 1 ] && open "$OLD"
'''


def install(new, wait_pid, log_path, reopen=True, old=None):
    """Start the swap in a detached helper; the caller then quits the app."""
    old = old or bundle_path()
    req = designated_requirement(old)
    if not req or req.startswith("cdhash"):
        raise RuntimeError("bản đang dùng chưa có chữ ký cố định")
    script = os.path.join(tempfile.mkdtemp(prefix="airmouse-update-"), "swap.sh")
    with open(script, "w") as f:
        f.write(SWAP)
    os.chmod(script, 0o700)
    subprocess.Popen(["/bin/bash", script, old, new, str(wait_pid), log_path, "1" if reopen else "0", req],
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
