"""Looks for a newer release on GitHub so the app can say so. Read-only: nothing is downloaded
or installed; the user opens the release page and updates by hand."""
import platform
import re
import threading

import requests

from version import VERSION

REPO = "thanhhaiwork1998/miband-airmouse"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE_PREFIX = f"https://github.com/{REPO}/"
CHECK_EVERY = 12 * 3600  # s; GitHub allows 60 unauthenticated requests an hour


def parse(version):
    """"v1.10.2" -> (1, 10, 2); None when it holds no number."""
    nums = re.findall(r"\d+", version or "")[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums)) if nums else None


def newer(remote, local=VERSION):
    a, b = parse(remote), parse(local)
    return a is not None and b is not None and a > b


def latest_release(timeout=10):
    """{"version", "title", "page", "download"} of the newest published release."""
    r = requests.get(LATEST, timeout=timeout, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"MiBand-AirMouse/{VERSION}"})
    r.raise_for_status()
    data = r.json()
    arch = "arm64" if platform.machine() == "arm64" else "x86_64"
    download = next((a.get("browser_download_url") for a in data.get("assets", [])
                     if a.get("name", "").endswith(f"-{arch}.zip")), None)
    page = data.get("html_url") or ""
    if not page.startswith(PAGE_PREFIX):
        page = PAGE_PREFIX + "releases/latest"  # only ever open this project's own pages
    if download and not download.startswith(PAGE_PREFIX):
        download = None
    return {"version": (data.get("tag_name") or "").lstrip("vV"), "title": data.get("name") or "",
            "page": page, "download": download}


class UpdateChecker:
    """One background thread for the app's lifetime: checks shortly after start and then every
    CHECK_EVERY while enabled() says so, or at once on check_now(). on_found(info) is called
    (from that thread) when a release newer than this one is out."""

    def __init__(self, on_found, log, enabled):
        self.on_found, self.log, self.enabled = on_found, log, enabled
        self.wake = threading.Event()
        self.forced = False
        self.checking = False

    def start(self, delay=3.0):
        threading.Thread(target=self._loop, args=(delay,), daemon=True).start()

    def check_now(self):
        self.forced = True
        self.wake.set()

    def _loop(self, delay):
        self.wake.wait(delay)
        while True:
            self.wake.clear()
            if self.enabled() or self.forced:
                self.forced, self.checking = False, True
                self._check()
                self.checking = False
            self.wake.wait(CHECK_EVERY)

    def _check(self):
        try:
            info = latest_release()
            if newer(info["version"]):
                self.log(f"update available: {info['version']} (this is {VERSION})")
                self.on_found(info)
            else:
                self.log(f"up to date ({VERSION}, latest {info['version']})")
        except Exception as e:  # offline, rate-limited...: try again later
            self.log(f"update check failed: {type(e).__name__}: {e}")
