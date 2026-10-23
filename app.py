"""MiBand AirMouse - menu bar app that turns a Xiaomi Smart Band 10 into an air mouse.

Point with the whole forearm (like a laser pointer) to move the cursor;
twist the forearm quickly (like turning a door knob) to click.
"""
import asyncio
import json
import math
import os
import signal
import subprocess
import threading
import time
import traceback
from collections import deque

import Quartz
import rumps
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory, NSEvent

from band import Band
from watch_source import WatchSource
from window import ControlWindow
from setup_window import SetupWindow
from updates import UpdateChecker
import installer
from laser import LaserDot
from version import VERSION
from PyObjCTools import AppHelper

HERE = os.path.dirname(os.path.abspath(__file__))
# The prebuilt .app is read-only (writing inside it breaks its signature), so its launcher
# points AIRMOUSE_DATA at ~/Library/Application Support; a source checkout keeps data here.
DATA = os.environ.get("AIRMOUSE_DATA", HERE)
os.makedirs(DATA, exist_ok=True)
CONFIG = os.path.join(DATA, "config.json")
LOG = os.path.join(DATA, "app.log")
for _path in (CONFIG, LOG):  # files from older versions were created readable by everyone
    if os.path.exists(_path):
        os.chmod(_path, 0o600)
REQUESTS = set()  # "show" / "quit" asked for by the Dock launcher's signals
PARENT = os.getppid()  # the Dock launcher; if it dies without signalling, don't linger
DEFAULT_PROFILE = "Mặc định"
PROFILE_SKIP = {"gestures_v2", "gestures_v3", "control_mode", "update_check", "skip_version"}  # the mode is a moment's choice, not tuning

# Jolt (m/s^2) above which a transient is a wrist tap rather than a pinch; must stay between
# the user's strongest pinch (1.2) and weakest tap (3.3).
DEFAULTS = {"speed": 600, "accel": 3.0, "deadzone": 0.05, "click_enabled": False, "click_threshold": 4.0,
            "swap_buttons": False, "axis_x": "z", "invert_x": True, "axis_y": "y", "invert_y": False}
AXIS = {"x": 0, "y": 1, "z": 2}
# "plane": gravity-aligned left/right + up/down, independent of how the arm is held.
# "axes": raw band axes (the first version).
DEFAULTS.update({"mode": "plane", "forearm_axis": "x", "invert_yaw": False, "invert_pitch": False,
                 "ignore_push": True, "push_threshold": 2.0,  # m/s^2 along the forearm
                 "anchor_enabled": True, "anchor_time": 1.0,
                 "tap_enabled": False, "tap_threshold": 2.5,  # m/s^2 jolt (taps measured 3.3-5.4)
                 "pinch_enabled": True, "pinch_threshold": 0.1,  # rad/s gyro jerk (rest <0.021, pinches 0.19-0.40)
                 "scroll_enabled": True, "scroll_speed": 1500, "scroll_invert": False,  # px per rad of flick
                 "smoothing": "Vua", "drag_scroll_enabled": True,
                 "control_mode": "mouse",  # or "slides" (presenter) / "media" (music, video)
                 "update_check": True, "skip_version": "",  # look for new releases on GitHub
                 "laser_enabled": True,  # presenter mode: the hand moves a red dot, not the cursor
                 "volume_invert": False,  # media mode: which way of turning the wrist is louder
                 "device": "miband"})  # or "applewatch"


def _add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def _sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _scale(a, k): return (a[0] * k, a[1] * k, a[2] * k)
def _dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def _norm(a): return _dot(a, a) ** 0.5
def _cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


GRAVITY = 9.81
MAX_DRIFT = 0.2  # rad/s; real gyro drift is far smaller (measured ~0.02) - more is actual motion
# The user's band rests at |drift| <= ~0.04 rad/s. A resting reading under PLAUSIBLE_DRIFT is
# drift; anything larger with low spread is a slow, deliberate aim and must not be absorbed.
PLAUSIBLE_DRIFT = 0.06
DRIFT_FOLLOW = 0.02  # closer than this: follow slowly; further: the stored value was wrong, jump
RESET_HOLD = 1.5     # s of proven rest before a wrong stored drift is replaced
REST_MAX_TILT = 0.02  # change of the gravity unit vector (~1 deg) allowed during a rest
ARM_RADIUS = 0.5  # m, generous elbow/shoulder-to-band distance for the centripetal allowance
ESCAPE_ANGLE = 0.06    # rad (~3.4 deg) of net arm turn that releases an anchored cursor
ESCAPE_LEAK = 1.0      # s; how quickly old travel is forgotten while anchored
RELEASE_GRACE = 0.15   # s a pending release waits for a wrist tap's jolt
TAP_REFRACTORY = 0.1   # s; ringing after a jolt is the same tap
TAP_GUARD = 0.6        # s after a wrist-tap jolt during which the arm's bounce is ignored
TAP_MIN_GAP, TAP_MAX_GAP = 0.15, 0.7  # s between the two halves of a double pinch / tap
RELEASE_RATIO = 0.45   # a 2nd pinch this much weaker than the 1st is the fingers letting go
# A wrist tap is a ~0.3s burst: a pinch-sized precursor ~0.1s before the main jolt, the jolt,
# then echoes. Collecting over 0.15s merges the burst into one event classified by its peak.
PLAYBACK_DELAY = 0.15  # s; one 0.1s batch of samples plus Bluetooth lateness (measured p97 ~50ms)
# The delay adapts to the Bluetooth link: what each batch needed (its own span + how late it
# arrived) at the DELAY_PCT percentile of the last ~30 s, plus a margin. Covering every batch
# would cost more delay than the fixed value (the link hiccups ~70 ms now and then), so the
# rare later batch just plays a few ms late; the delay follows the link up fast, down slowly.
DELAY_MIN, DELAY_MAX = 0.09, 0.30
DELAY_PCT, DELAY_MARGIN, DELAY_WINDOW = 0.90, 0.005, 300
FRAME_RATE = 120       # cursor updates per second
# Smoothing levels: (cutoff Hz when the hand is still, extra Hz per rad/s of speed)
SMOOTHING = {"Tat": None, "Vua": (3.0, 10.0), "Manh": (1.5, 6.0)}
PALM_UP_ENTER, PALM_UP_EXIT = -0.8, -0.5  # cos of roll from the normal posture (~145 / 120 deg)
PALM_UP_HOLD = 0.3     # s the turned-over posture must be held before scroll mode starts
FLICK_RATE = 1.0       # rad/s of upward hand speed that counts as a scroll flick
SCROLL_STEPS = 16      # frames a flick's scroll is spread over (~0.2s)
PINCH_MAX_RATE, TAP_MAX_RATE = 0.8, 3.0  # rad/s of band rotation while a gesture may hold the anchor
# Accepting a pinch is looser: in use the user's pinches turned the band up to ~1 rad/s with
# an average turn up to ~0.3 (stronger than when measured at rest).
PINCH_ACCEPT_RATE, PINCH_ACCEPT_DRIFT = 1.0, 0.32
SPIN_SETTLE = 0.4      # s after a fast turn during which no gesture may start
TRIPLE_GAP = 0.45      # s allowed between pinches after the 2nd (a double click waits this long)
SCROLL_PINCHES = 4     # pinches in a row that switch scroll mode on
SCROLL_EXIT_RATE = 4.0  # rad/s: a flick this fast leaves scroll mode (scrolling ~0.5-1, taps <=1.9)
SCROLL_EXIT_FREEZE = 0.4  # s the cursor holds still after the exit flick (the arm's bounce)
FLICK_UNDO = 0.3       # s of scrolling before the exit flick that is taken back
# Scroll mode is a joystick: the page keeps scrolling while the hand is held above or below
# where it was when scroll mode started, faster the further it is.
JOY_DEAD = 0.05        # rad (~3 deg) of height change that doesn't scroll (resting hand, pinches)
JOY_CURVE = 1.7        # speed ~ (angle past JOY_DEAD)^JOY_CURVE: a slight tilt reads, a big one races
JOY_GAIN = 3.0         # speed in px/s = scroll_speed * JOY_GAIN * angle^JOY_CURVE
JOY_MAX = 0.8          # rad; tilting further doesn't scroll faster
# Presenter / media modes: the cursor stays put; a quick swipe of the hand is a command.
SWIPE_START = 0.8      # rad/s: hand speed above which a stroke is being collected
SWIPE_RATE = 1.5       # rad/s peak a stroke needs to be a swipe (relaxed pointing ~0.5-1.5)
SWIPE_ANGLE = 0.2      # rad (~11 deg) a swipe must travel
SWIPE_RATIO = 1.5      # the main direction must be this much larger than the other
SWIPE_GUARD = 0.8      # s after a swipe in which the hand's way back is ignored
SWIPE_WINDUP = 0.2     # s of a swipe's start, below SWIPE_START, that already moved the red dot
VOLUME_STEPS = 2       # volume key presses per swipe up/down
LASER_SHOW = 3.0       # s the presenter's red dot stays after the hand stops (then fades)
YAW_LEAK = 120.0       # s; the arm view's left/right angle has no absolute reference (no compass),
                       # so it drifts slowly back to centre instead of drifting away with the gyro
# Media mode's volume knob (two pinches on/off): turning the wrist steps the volume
VOLUME_TURN = 0.1      # rad (~6 deg) of wrist turn per volume step (macOS has 16)
VOLUME_DEAD = 0.1      # rad/s of turning ignored (tremor, slow drift)
VOLUME_IDLE = 10.0     # s without turning after which the knob switches itself off
KEY_RIGHT, KEY_LEFT = 124, 123        # macOS virtual key codes
MEDIA_PLAY, MEDIA_NEXT, MEDIA_PREVIOUS = 16, 17, 18   # NX_KEYTYPE_* (ev_keymap.h)
MEDIA_SOUND_UP, MEDIA_SOUND_DOWN = 0, 1
SERIES_SETTLE = 0.25   # s after a toggle/click in which pinch echoes can't start a new series
PINCH_CONFIRM = 0.12   # s after a pinch in which the hand must stay put for it to count
CONFIRM_RATE = 0.35    # rad/s: average band rotation in that time above which it was a sweep starting
PINCH_MAX_DRIFT = 0.22  # rad/s; average band rotation over a pinch: 0.01-0.14 measured in use,
                        # a sweep starting/stopping 0.32+
EVENT_WINDOW = 0.15    # s over which a transient's peak jolt and jerk are collected
TAP_RINGING = 0.35     # s after a wrist tap during which any transient is its echo (measured burst ~0.35s)
STILL_WINDOW = 30  # gyro samples (~0.3s)
STILL_STD = 0.05   # gyro spread below which the hand counts as resting (a resting arm measured ~0.03)


def _stillness(samples):
    """(is the hand resting?, mean gyro) over the recent window. A resting hand still shows a
    steady offset (drift) but almost no spread; a moving hand always has spread."""
    if len(samples) < STILL_WINDOW:
        return False, None
    n = len(samples)
    mean = tuple(sum(v[i] for v in samples) / n for i in range(3))
    return _spread(samples, mean) < STILL_STD, mean


def _spread(samples, mean=None):
    n = len(samples) or 1
    mean = mean or tuple(sum(v[i] for v in samples) / n for i in range(3))
    return max((sum((v[i] - mean[i]) ** 2 for v in samples) / n) ** 0.5 for i in range(3))


def _hand_rates(w, up, forearm_axis):
    """(sideways, downward) speed of the band for arm rotation rate w, in band coordinates,
    per unit arm length. Which sideways sign means "right" depends on how the band sits and
    is learned by the calibration (invert_yaw / invert_pitch)."""
    f = [0.0, 0.0, 0.0]
    f[forearm_axis] = 1.0
    v = _cross(w, f)                                   # band velocity direction
    fh = _sub(f, _scale(up, _dot(f, up)))              # forearm projected on the floor
    n = _norm(fh)
    side = _dot(v, _scale(_cross(up, fh), 1 / n)) if n > 0.2 else 0.0  # arm pointing straight up/down
    return side, -_dot(v, up)


class Smoother:
    """Speed-adaptive low-pass on the pointing rate (the idea of the 1-Euro filter): slow,
    precise aiming is filtered hard to remove hand tremor, while fast sweeps raise the
    cutoff so they don't lag."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.x = self.y = None

    def __call__(self, x, y, dt, params):
        if not params:
            return x, y
        if self.x is None:
            self.x, self.y = x, y
            return x, y
        min_cutoff, beta = params
        cutoff = min_cutoff + beta * (x * x + y * y) ** 0.5  # Hz
        tau = 1 / (2 * math.pi * cutoff)
        a = dt / (dt + tau)
        self.x += a * (x - self.x)
        self.y += a * (y - self.y)
        return self.x, self.y


def _acceleration(rate, strength):
    """Pointer acceleration like macOS: slow arm motion keeps the base gain (precise aiming),
    fast flicks multiply it, so a short quick flick can cross the whole screen."""
    return min(1.0 + strength * rate * rate, 1.0 + 8.0 * strength)


def _soft_deadzone(v, dz):
    # Subtract the dead zone instead of cutting at it, so movement starts smoothly from zero.
    return 0.0 if abs(v) <= dz else v - dz if v > 0 else v + dz


def private_open(path, mode):
    """Open for writing with owner-only permissions from the first byte (the log tells when
    and how the band is used; config.json holds its token)."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC)
    return os.fdopen(os.open(path, flags, 0o600), mode)


def log(msg):
    with private_open(LOG, "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def load_config():
    cfg = {}
    if os.path.exists(CONFIG):  # a fresh install has none yet: the setup window creates it
        with open(CONFIG) as f:
            cfg = json.load(f)
    settings = dict(DEFAULTS)
    saved = cfg.get("mouse", {})
    settings.update(saved)
    if saved and "invert_yaw" not in saved:
        settings["mode"] = "axes"  # plane mode needs one calibration to learn its signs
    if saved and "pinch_threshold" not in saved:
        settings["tap_threshold"] = DEFAULTS["tap_threshold"]  # now also separates taps from pinches
    if not saved.get("gestures_v2"):
        # Forearm roll during normal pointing kept firing twist right-clicks; right click is the
        # double wrist tap now, twist stays available in the menu.
        settings["click_enabled"] = False
        settings["gestures_v2"] = True
    if not saved.get("gestures_v3"):
        # Hard finger presses were firing wrist-tap right clicks; the user turned right click off.
        settings["tap_enabled"] = False
        for prof in cfg.get("profiles", {}).values():
            prof["tap_enabled"] = False
        settings["gestures_v3"] = True
    if not cfg.get("profiles"):
        cfg["profiles"] = {DEFAULT_PROFILE: profile_of(settings)}
        cfg["active_profile"] = DEFAULT_PROFILE
    return cfg, settings


def profile_of(settings):
    """The part of the settings a profile keeps: every tunable and the calibration (it
    depends on who wears the band and how), not internal migration markers."""
    return {k: v for k, v in settings.items() if k not in PROFILE_SKIP}


def save_config(cfg, settings):
    cfg["mouse"] = settings
    tmp = CONFIG + ".tmp"  # written whole, then swapped in: never half-written, never readable by others
    with private_open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG)


class Pointer:
    """Turns gyro batches into cursor movement and twist clicks."""

    def __init__(self, settings):
        self.s = settings
        self.paused = False
        self.bias = None
        self.calib = []
        self.calib_start = time.monotonic()
        self.queue = deque()      # (play time, dx, dy) per sensor sample
        self.cursor = None        # our sub-pixel cursor position
        self.clock_offset = None  # local time minus band time
        self.delay = PLAYBACK_DELAY  # adapted from the batches' lateness (see DELAY_PCT)
        self.needs = deque(maxlen=DELAY_WINDOW)
        self.delay_logged = 0.0
        self.last_dev_ts = None
        self.smoother = Smoother()
        self.last_batch = None
        self.interval = 0.1
        self.twist_dir = 0
        self.freeze_until = 0.0
        self.last_twist = 0
        self.return_until = 0.0
        self.recent = []  # last few gyro samples, for stillness detection
        self.rest = None  # running proof of a genuine rest (see _check_rest)
        self.push_until = 0.0
        self.live_push = 0.0
        self.anchored = False
        self.last_motion = 0.0
        self.motion = (0.0, 0.0)  # smoothed pointing rate
        self.escape = []        # [dx, dy] leaky net travel (rad) while anchored; [] = none
        self.release_at = None  # when a pending anchor release takes effect
        self.a_smooth = None    # low-passed accel, to see the sharp jolt of a tap
        self.live_tap = 0.0
        self.live_pinch = 0.0
        self.prev_w = None
        self.event = None       # [start, max jolt, max gyro jerk] of a transient being classified
        self.last_event = 0.0
        self.last_tap = 0.0
        self.first = {}         # gesture kind -> (time, jerk) of its first half (awaiting the second)
        self.tap_guard_until = 0.0
        self.capture = None     # list of (t, ax, ay, az, gx, gy, gz) while the pinch test records
        self.spin_until = 0.0
        self.dragging = False      # scroll mode (toggled by four pinches)
        self.joy_ref = None        # forearm height (up . forearm axis) when scroll mode started
        self.joy_drop = 0.0        # integrated hand drop, when gravity can't tell (sensor-axis mode)
        self.stroke = None         # [dx, dy, peak speed, start play time] of a possible swipe
        self.swipe_guard = 0.0
        self.live_motion = (0.0, 0.0)  # smoothed hand speed in screen directions, for the arm view
        self.pinch_flash = 0.0     # when the last pinch was felt (the arm view flashes)
        self.yaw = 0.0             # sideways arm angle for the arm view, rad (+ = right), see YAW_LEAK
        self.volume_mode = False   # media mode: the wrist is a volume knob
        self.volume_turn = 0.0     # rad turned since the last volume step
        self.volume_since = 0.0    # last volume step (or switching the knob on)
        self.laser = None          # presenter's red dot (global screen points), moved by run()
        self.laser_moved = 0.0     # when it last moved (the overlay fades it out after LASER_SHOW)
        self.laser_rewind = None   # play time to put the dot back to (a swipe isn't pointing)
        self.laser_hist = deque()  # (play time, position) for that rewind
        self.screens = (0.0, None)  # (when measured, union of the displays' bounds)
        self.pending_click = None  # time a double pinch turns into a click unless a 3rd pinch comes
        self.settle_until = 0.0
        self.tentative = None     # a pinch awaiting confirmation that the hand stayed put
        self.palm_up = False
        self.palm_since = None  # when the hand started looking turned over
        self.flick = 0.0        # rad of upward flick collected so far
        self.scroll_pending = []
        self.scroll_rest = 0.0  # sub-pixel remainder
        self.last_click = (0.0, None, 0)
        self.last_peak = 0.0
        self.recording = None  # used by calibration
        self.grav = None  # unit "up" vector in band coordinates, tracked from accel + gyro

    def _check_rest(self, still, gyro, now):
        """Replace a wrong stored drift (e.g. measured during a slow move) - but only on proof
        of a real rest: still for RESET_HOLD with an unchanging reading of drift size and the
        band not tilting. A slow, deliberate aim fails at least one of these, so it is never
        swallowed."""
        if not still or self.grav is None:
            self.rest = None
            return
        if self.rest is None:
            self.rest = {"start": now, "sum": (0.0, 0.0, 0.0), "n": 0, "grav": self.grav}
        r = self.rest
        for g in gyro:
            r["sum"] = _add(r["sum"], g)
        r["n"] += len(gyro)
        if _norm(_sub(self.grav, r["grav"])) > REST_MAX_TILT:
            self.rest = None  # the band is turning (e.g. a slow up/down aim): not a rest
            return
        if now - r["start"] < RESET_HOLD:
            return
        rest_mean = _scale(r["sum"], 1 / r["n"])
        if _norm(rest_mean) < PLAUSIBLE_DRIFT and _norm(_sub(rest_mean, self.bias)) >= DRIFT_FOLLOW:
            log(f"bias reset {self.bias} -> {rest_mean}")
            self.bias = rest_mean
        self.rest = None

    def rezero(self):
        """Forget the gyro drift; it is re-measured the next time the hand rests."""
        self.bias, self.recent = None, []
        self.yaw = 0.0
        self.queue.clear()
        self.smoother.reset()
        self.calib_start = time.monotonic()

    def twist_axis(self):
        if self.s["mode"] == "plane":
            return AXIS[self.s["forearm_axis"]]
        return ({0, 1, 2} - {AXIS[self.s["axis_x"]], AXIS[self.s["axis_y"]]}).pop()

    def _track_gravity(self, w, a, dt):
        """Complementary filter: rotate the up vector with the gyro, pull it toward the accel
        reading only when the arm isn't accelerating (|a| close to 1 g)."""
        if self.grav is None:
            n = _norm(a)
            self.grav = _scale(a, 1 / n) if n > 1e-3 else (0.0, 0.0, 1.0)
            return
        g = _sub(self.grav, _scale(_cross(w, self.grav), dt))
        n = _norm(a)
        if 7.0 < n < 12.5:
            g = _add(_scale(g, 0.98), _scale(a, 0.02 / n))
        self.grav = _scale(g, 1 / _norm(g))

    def along_forearm(self, a):
        """Linear acceleration (gravity removed) along the forearm axis, m/s^2."""
        return _sub(a, _scale(self.grav, GRAVITY))[AXIS[self.s["forearm_axis"]]]

    def _pushing(self, a, w, now):
        """Reaching forward or pulling back bends the elbow, which tilts the forearm and would
        read as up/down. It shows up as linear acceleration along the forearm (gravity removed),
        which deliberate pointing barely produces - so freeze the cursor while it lasts."""
        if not self.s["ignore_push"] or self.grav is None:
            return False
        f = AXIS[self.s["forearm_axis"]]
        along = self.along_forearm(a)
        # Any arm rotation adds centripetal acceleration toward the elbow (~ rate^2 * radius),
        # e.g. during a quick sideways flick; it must not count as pulling back.
        spin = _dot(w, w) - w[f] * w[f]
        allowance = ARM_RADIUS * spin
        thr = self.s["push_threshold"]
        hand = self.s.get("hand_dir", 0)  # +1/-1: which way along the axis points to the hand
        if hand:
            toward_hand = along * hand
            pushing = toward_hand > thr or toward_hand < -(thr + allowance)
        else:
            pushing = abs(along) > thr + allowance
        self.live_push = max(self.live_push * 0.98, abs(along))
        if pushing:
            if now >= self.push_until:
                log(f"push ignored ({along:.1f} m/s2)")
            self.push_until = now + 0.35  # covers the braking at the end of the reach
        return now < self.push_until

    def plane_rates(self, w):
        """Follow where the band travels, not how the arm rotates: the band's velocity is
        w x forearm (per unit arm length), split into room-horizontal (sideways) and vertical.
        So sweeping from the shoulder, wiping from a planted elbow or swinging from the wrist
        all move the cursor the way the hand moves; rolling the forearm moves the band nowhere
        and is ignored."""
        return _hand_rates(w, self.grav, AXIS[self.s["forearm_axis"]])

    def __call__(self, batch):
        now = time.monotonic()
        if self.last_batch:
            self.interval = 0.8 * self.interval + 0.2 * min(now - self.last_batch, 1.0)
        self.last_batch = now
        gyro = [(s.x, s.y, s.z) for s in batch.gyro]
        accel = [(s.x, s.y, s.z) for s in batch.accel]
        if not gyro:
            return
        dt = self.interval / len(gyro)
        if self.capture is not None:  # raw recording for the pinch test
            for i, g in enumerate(gyro):
                a = accel[min(i, len(accel) - 1)] if accel else (0.0, 0.0, 0.0)
                self.capture.append((now - (len(gyro) - 1 - i) * dt,) + tuple(a) + tuple(g))
        self.recent = (self.recent + gyro)[-STILL_WINDOW:]
        still, mean = _stillness(self.recent)
        if self.bias is None:  # wait until the hand is genuinely still, then measure gyro drift
            if accel:
                self.grav = None
                self._track_gravity((0, 0, 0), accel[-1], dt)
            waited = now - self.calib_start
            # After 8s settle for "fairly still", but never measure drift from a moving hand -
            # a bias taken mid-motion makes the cursor fly and can't anchor.
            calm = mean and _spread(self.recent, mean) < 3 * STILL_STD
            plausible = mean and _norm(mean) < PLAUSIBLE_DRIFT
            if plausible and (still and waited > 1 or calm and waited > 8) or \
                    still and waited > 15 and _norm(mean) < MAX_DRIFT:  # unusually large real drift
                self.bias = mean
                log(f"bias={self.bias} still={still} spread={_spread(self.recent):.4f} up={self.grav}")
            return
        if still and _norm(mean) < PLAUSIBLE_DRIFT and _norm(_sub(mean, self.bias)) < DRIFT_FOLLOW:
            # Resting and close to the stored drift: drift wanders with temperature, follow it
            # slowly (time constant ~2s).
            self.bias = _add(_scale(self.bias, 0.95), _scale(mean, 0.05))
        self._check_rest(still, gyro, now)

        mouse = self.s["control_mode"] == "mouse"
        if not mouse and (self.dragging or self.palm_up):
            self.dragging = self.palm_up = False  # scrolling belongs to mouse mode
        if self.volume_mode and self.s["control_mode"] != "media":
            self._set_volume_mode(False, now)
        if mouse:
            self._update_palm(now)
        ax, ay, at = AXIS[self.s["axis_x"]], AXIS[self.s["axis_y"]], self.twist_axis()
        plane = self.s["mode"] == "plane"
        thr = self.s["click_threshold"]
        dx = dy = 0.0
        times = self._sample_times(batch.gyro, now, dt)
        for i, v in enumerate(gyro):
            t_play, dt = times[i]  # when the sample was measured (local clock), its own interval
            w = _sub(v, self.bias)
            ts = now - (len(gyro) - 1 - i) * dt  # this sample's time
            a = accel[min(i, len(accel) - 1)] if accel else None
            if a:
                self._track_gravity(w, a, dt)
            if self.recording is not None:
                yaw, pitch = self.plane_rates(w)
                rec = self.recording
                rec["raw"] = _add(rec["raw"], w)
                rec["abs"] = _add(rec["abs"], (abs(w[0]), abs(w[1]), abs(w[2])))
                rec["yaw"] += yaw
                rec["pitch"] += pitch
                if accel:
                    a = accel[min(i, len(accel) - 1)]
                    rec["linear"] = _add(rec["linear"], _sub(a, _scale(self.grav, GRAVITY)))
                    rec["n"] += 1
                continue
            tapping = self._gesture(a, w, ts) if a else False
            if self._flick_exit(w, ts, t_play):
                continue
            if self.volume_mode:
                self._volume_knob(w[at], ts, dt)
                continue
            twist = w[at]
            self.last_peak = max(self.last_peak * 0.999, abs(twist))
            if self.s["click_enabled"] and not tapping:
                direction = 1 if twist > 0 else -1
                returning = now < self.return_until and direction == -self.last_twist
                if not self.twist_dir and abs(twist) > thr and not returning:
                    self.twist_dir = direction
                elif self.twist_dir and abs(twist) < thr / 3:
                    self._click(self.twist_dir)
                    # Turning the forearm back is a twist the other way - don't click it.
                    self.last_twist, self.return_until = self.twist_dir, now + 0.5
                    self.twist_dir = 0
                    self.freeze_until = now + 0.25
            if now < self.return_until:
                continue  # the return twist must not drag the cursor either
            if self.twist_dir or now < self.freeze_until:
                continue  # don't let the twist drag the cursor
            if accel and not self.palm_up and self._pushing(accel[min(i, len(accel) - 1)], w, now):
                continue  # reaching forward / pulling back is not a pointing motion
            if plane:
                gx, gy = self.plane_rates(w)
                inv_x, inv_y = self.s["invert_yaw"], self.s["invert_pitch"]
            else:
                gx, gy = w[ax], w[ay]
                inv_x, inv_y = self.s["invert_x"], self.s["invert_y"]
            sx, sy = gx * (-1 if inv_x else 1), gy * (-1 if inv_y else 1)  # screen right / down
            lm = self.live_motion
            self.live_motion = (0.85 * lm[0] + 0.15 * sx, 0.85 * lm[1] + 0.15 * sy)
            self.yaw = max(-math.pi, min(math.pi, self.yaw * math.exp(-dt / YAW_LEAK) + sx * dt))
            if not mouse:
                # Presenter / media: the cursor stays put. Swipes are commands; the anchor still
                # runs, so a resting hand's double pinch is heard.
                self._swipe(gx * (-1 if inv_x else 1), gy * (-1 if inv_y else 1), ts, dt, t_play)
                if not self.laser_on() or ts < self.swipe_guard:
                    self._anchored(gx, gy, ts, dt, tapping)
                    continue
                # Presenter with the red dot: the hand moves the dot (run() sends the motion
                # there instead of to the cursor), through the same anchor/smoothing as the mouse.
            if self.palm_up:
                # Palm turned up: the cursor holds still and upward flicks scroll the page.
                self._scroll_flick(gy * (1 if inv_y else -1), dt)  # physical upward speed
                continue
            if self._drag_scroll(gx * (-1 if inv_x else 1), gy * (-1 if inv_y else 1), ts, dt, t_play):
                continue
            if self._anchored(gx, gy, ts, dt, tapping):
                continue
            gx, gy = self.smoother(gx, gy, dt, SMOOTHING.get(self.s["smoothing"]))
            gx, gy = _soft_deadzone(gx, self.s["deadzone"]), _soft_deadzone(gy, self.s["deadzone"])
            gain = self.s["speed"] * _acceleration((gx * gx + gy * gy) ** 0.5, self.s["accel"])
            if not self.paused:
                # Played back at the time it was measured (see run), so motion keeps the
                # sensor's own rhythm however unevenly the Bluetooth batches arrive.
                self.queue.append((t_play, gx * dt * gain * (-1 if inv_x else 1),
                                   gy * dt * gain * (-1 if inv_y else 1), dt, 0.0))

    def _sample_times(self, samples, now, fallback_dt):
        """(local playback time, interval) per sample from the band's microsecond timestamps.
        The clock offset is the smallest arrival delay seen (least Bluetooth latency)."""
        ts = [s.timestamp for s in samples]
        n = len(ts)
        if not all(ts) or any(b <= a for a, b in zip(ts, ts[1:])):
            return [(now - (n - 1 - i) * fallback_dt, fallback_dt) for i in range(n)]
        offset = now - ts[-1] / 1e6
        if self.clock_offset is None or offset < self.clock_offset or offset - self.clock_offset > 1.0:
            self.clock_offset = offset  # also resyncs after a reconnect/gap
        else:
            self.clock_offset += 0.0002  # follow slow drift between the band's and the Mac's clock
        prev = self.last_dev_ts
        if not prev or not 0 < ts[0] - prev < 200_000:
            prev = ts[0] - fallback_dt * 1e6
        # Its first sample starts playing at the start of its own interval, so this batch must
        # be in hand that long before its last sample's play time - plus however late it came.
        self._adapt_delay((ts[-1] - prev) / 1e6 + max(0.0, offset - self.clock_offset), now)
        out = []
        for t in ts:
            out.append((t / 1e6 + self.clock_offset, min(max((t - prev) / 1e6, 0.001), 0.05)))
            prev = t
        self.last_dev_ts = ts[-1]
        return out

    def _adapt_delay(self, need, now):
        self.needs.append(need)
        if len(self.needs) < 30:
            return  # too few batches to judge yet: keep the safe default
        ranked = sorted(self.needs)
        target = min(max(ranked[int(DELAY_PCT * (len(ranked) - 1))] + DELAY_MARGIN, DELAY_MIN), DELAY_MAX)
        self.delay += (target - self.delay) * (0.2 if target > self.delay else 0.02)
        if now - self.delay_logged > 30:
            self.delay_logged = now
            log(f"playback delay {self.delay * 1000:.0f} ms (median need {ranked[len(ranked) // 2] * 1000:.0f}, "
                f"p{DELAY_PCT * 100:.0f} {ranked[int(DELAY_PCT * (len(ranked) - 1))] * 1000:.0f}, max {ranked[-1] * 1000:.0f})")

    def _palm_dir(self):
        """Unit direction of gravity across the forearm (which way the palm faces), or None
        when the forearm points too steeply up/down to tell."""
        f = AXIS[self.s["forearm_axis"]]
        across = list(self.grav)
        across[f] = 0.0
        n = _norm(across)
        return _scale(across, 1 / n) if n > 0.5 else None

    def _update_palm(self, now):
        """Palm up = forearm rolled about half a turn from the user's normal (palm-down)
        pointing posture, seen as gravity flipping across the forearm. Hysteresis between
        ~107 and 90 degrees so the mode doesn't flicker at the boundary."""
        if not self.s["scroll_enabled"] or self.grav is None:
            self.palm_up = False
            return
        d = self._palm_dir()
        if d is None:
            return
        ref = self.s.get("palm_ref")
        if ref is None:
            self.s["palm_ref"] = list(d)  # first look at the arm: the normal pointing posture
            log(f"palm reference {d}")
            return
        c = _dot(d, ref)
        if not self.palm_up:
            # Only a deliberate, held turn-over enters scroll mode: the forearm rolls in passing
            # while pointing, and the user's normal posture is already tilted.
            if c >= PALM_UP_ENTER:
                self.palm_since = None
            elif self.palm_since is None:
                self.palm_since = now
            elif now - self.palm_since >= PALM_UP_HOLD:
                self.palm_up, self.anchored, self.flick, self.first = True, False, 0.0, {}
                log(f"palm up: scroll mode (cos={c:.2f})")
        elif c > PALM_UP_EXIT:
            self.palm_up, self.last_motion, self.palm_since = False, now, None
            log("palm down: pointer mode")

    def _scroll_flick(self, up, dt):
        """Fast upward motion while the palm is up scrolls the page down, by how far the hand
        flicked. Motion below FLICK_RATE (lowering the hand back slowly) doesn't scroll, so
        flicks can be repeated."""
        if up > FLICK_RATE:
            self.flick += up * dt
        elif self.flick and up < FLICK_RATE / 2:
            px = self.flick * self.s["scroll_speed"]
            self.flick = 0.0
            sign = 1 if self.s["scroll_invert"] else -1  # negative wheel delta = page moves down
            weights = list(range(SCROLL_STEPS, 0, -1))   # ease out, like momentum scrolling
            total = sum(weights)
            self.scroll_pending += [sign * px * wt / total for wt in weights]
            log(f"scroll {px:.0f}px")

    def _drag_scroll(self, sx, sy, ts, dt, t_play):
        """Scroll mode, switched on by four pinches: the cursor stays put and the page scrolls
        on its own while the hand is held above (scroll up) or below (scroll down) where it
        was when the mode started - slowly for a slight tilt, faster the further it goes.
        A fast flick of the hand leaves it.
        sx, sy: pointing rates in screen directions (sy > 0 = hand moving down).
        Returns True while scroll mode owns the sample."""
        if not self.dragging:
            return False
        drop = self._hand_drop(sy, dt)
        excess = min(abs(drop) - JOY_DEAD, JOY_MAX)
        if excess > 0 and not self.paused:
            angle = math.copysign(JOY_GAIN * excess ** JOY_CURVE * dt, drop)
            self.queue.append((t_play, 0.0, 0.0, dt, self._drag_pixels(angle)))
        return True

    def _hand_drop(self, sy, dt):
        """How far the hand is below where scroll mode started (rad, screen sense: + = lower).
        In plane mode it is read from gravity along the forearm, so it never drifts: the rate
        of change of up . forearm is exactly minus the vertical rate plane_rates reports, so
        the calibrated invert_pitch applies the same way. Otherwise the rate is integrated."""
        if self.s["mode"] == "plane" and self.grav is not None and self.joy_ref is not None:
            rise = self.grav[AXIS[self.s["forearm_axis"]]] - self.joy_ref
            return -rise * (-1 if self.s["invert_pitch"] else 1)
        self.joy_drop += sy * dt
        return self.joy_drop

    def _flick_exit(self, w, ts, t_play):
        """In scroll mode a fast flick of the hand (any direction) leaves it. Checked before the
        reach/twist filters so they can't swallow the flick. The scrolling its wind-up already
        queued is taken back, and the cursor holds still until the arm stops swinging."""
        if not self.dragging or _norm(w) <= SCROLL_EXIT_RATE:
            return False
        self.queue = deque(e for e in self.queue if not (e[4] and e[0] >= t_play - FLICK_UNDO))
        self._set_scroll_mode(False, ts)
        # The hand is moving on - leave the cursor free rather than anchored
        self.anchored, self.last_motion = False, ts + SCROLL_EXIT_FREEZE
        self.smoother.reset()
        self.freeze_until = ts + SCROLL_EXIT_FREEZE
        self.first, self.pending_click = {}, None
        return True

    def _set_scroll_mode(self, on, ts):
        if on:
            self.dragging, self.anchored = True, False
            self.joy_drop = 0.0
            self.joy_ref = self.grav[AXIS[self.s["forearm_axis"]]] if self.grav is not None else None
            log("scroll mode on")
        else:
            # Back to the anchored cursor at the same spot, ready to click
            self.dragging, self.anchored, self.escape, self.last_motion = False, True, [], ts
            log("scroll mode off")

    def _drag_pixels(self, down_angle):
        """Page scroll for a vertical hand movement (rad, positive = down): hand up scrolls up."""
        return -down_angle * self.s["scroll_speed"] * (-1 if self.s["scroll_invert"] else 1)

    def _anchored(self, gx, gy, ts, dt, tapping):
        """After the hand rests for anchor_time the cursor locks in place, so small tremor and
        the jolt of tapping can't nudge it off the target. A deliberate move (net turn of
        ESCAPE_ANGLE of net travel, see below) releases it."""
        if not self.s["anchor_enabled"] and self.s["control_mode"] == "mouse":
            self.anchored = False
            return False
        # Judge rest on ~0.1s-averaged motion: single samples of hand tremor must not keep
        # restarting the countdown.
        self.motion = (0.9 * self.motion[0] + 0.1 * gx, 0.9 * self.motion[1] + 0.1 * gy)
        if not self.anchored:
            if _norm((self.motion[0], self.motion[1], 0.0)) > self.s["deadzone"]:
                self.last_motion = ts
            elif ts - self.last_motion > self.s["anchor_time"]:
                self.anchored, self.escape = True, []
                log("anchored")
            return False
        ev = self.event
        # Pinch-like while the band only shakes in place (low average turn); a hand that keeps
        # turning one way is moving, however gently it started.
        gesture_like = ev is not None and (ev[1] >= self.s["tap_threshold"] or
                                           (ev[3] <= PINCH_MAX_RATE and _norm(_scale(ev[4], 1 / ev[5])) <= PINCH_MAX_DRIFT))
        if tapping or gesture_like:
            self.release_at = None
            # A gesture is being told apart (or a tap's bounce is settling): hold the anchor, and
            # don't count this motion as travel - otherwise the gesture is dropped half-way.
            # Once the band turns faster than any pinch, it's the hand moving: let go.
            self.escape = []
            return True
        # Net travel since anchoring, slowly forgotten: tremor cancels itself out, while a slow
        # but deliberate move keeps adding up until it releases the anchor.
        ex, ey = self.escape if self.escape else (0.0, 0.0)
        k = math.exp(-dt / ESCAPE_LEAK)
        ex, ey = ex * k + gx * dt, ey * k + gy * dt
        self.escape = [ex, ey]
        if (ex * ex + ey * ey) ** 0.5 < ESCAPE_ANGLE:
            self.release_at = None
            return True
        # A wrist tap winds up with a small move just before its jolt: wait a moment so the
        # jolt can still claim the anchor, then let go.
        if self.release_at is None:
            self.release_at = ts + RELEASE_GRACE
        if ts < self.release_at:
            return True
        self.release_at = None
        self.anchored, self.last_motion = False, ts
        self.smoother.reset()  # its state is from before the anchor; start fresh
        log("released")
        return False

    def _gesture(self, a, w, ts):
        """Finger pinches and wrist taps both reach the band as short, sharp transients - unlike
        the smooth motion of pointing. Measured on the user's arm: a pinch shows as a gyro
        jerk 9-19x the resting noise with only a small accel jolt (0.6-1.2 m/s2); a wrist tap
        has a big jolt (3-5 m/s2). So a transient is classified over a short window by its jolt.
        Two pinches = left click, two taps = right click, only while the cursor is anchored.
        Returns True while a gesture is in progress (its bounce must not move or twist-click)."""
        self.a_smooth = a if self.a_smooth is None else _add(_scale(self.a_smooth, 0.9), _scale(a, 0.1))
        jolt = _norm(_sub(a, self.a_smooth))
        jerk = _norm(_sub(w, self.prev_w)) if self.prev_w else 0.0
        self.prev_w = w
        self.live_tap = max(self.live_tap * 0.995, jolt)
        self.live_pinch = max(self.live_pinch * 0.995, jerk)
        if self.tentative is not None:
            t = self.tentative
            t["sum"], t["n"] = t["sum"] + _norm(w), t["n"] + 1
            if ts >= t["until"]:
                self.tentative = None
                if t["sum"] / t["n"] < CONFIRM_RATE:
                    self._count("pinch", t["start"], t["jerk"])
                else:
                    log(f"pinch discarded: the hand kept moving ({t['sum'] / t['n']:.2f} rad/s)")
        if self.pending_click is not None and ts >= self.pending_click:
            # Two pinches and no third in time: a click after all (media: the volume knob)
            self.pending_click, self.first = None, {}
            if self.s["control_mode"] == "media":
                self._command("pinch")
            else:
                self._press(right=False)
            self.settle_until = ts + SERIES_SETTLE
        if not (self.anchored or self.dragging or self.volume_mode):  # those modes listen for their exit pinches
            self.first, self.event, self.pending_click, self.tentative = {}, None, None, None
            return False
        tap_thr, pinch_thr = self.s["tap_threshold"], self.s["pinch_threshold"]
        rate = _norm(w)
        if rate > TAP_MAX_RATE:
            self.spin_until = ts + SPIN_SETTLE  # the arm is turning over; its stop isn't a gesture
        if self.event is None:
            if ts < self.spin_until:
                return ts < self.tap_guard_until
            # A pinch barely turns the band; a jerk while the arm is already moving is just
            # motion. (A wrist tap still counts by its jolt.)
            pinchy = jerk > pinch_thr and rate < PINCH_ACCEPT_RATE
            if (pinchy or jolt > tap_thr) and ts - self.last_event > TAP_REFRACTORY:
                self.event = [ts, jolt, jerk, rate, w, 1]
                if jolt > tap_thr:
                    # A tap's bounce could drag the anchor off; a pinch is too small to.
                    self.tap_guard_until, self.escape = ts + TAP_GUARD, []
        else:
            ev = self.event
            ev[1], ev[2], ev[3] = max(ev[1], jolt), max(ev[2], jerk), max(ev[3], rate)
            ev[4], ev[5] = _add(ev[4], w), ev[5] + 1
            if jolt > tap_thr and self.tap_guard_until < ts:
                self.tap_guard_until, self.escape = ts + TAP_GUARD, []
            if ts - self.event[0] >= EVENT_WINDOW:
                self._classify(*self.event)
                self.event = None
        return ts < self.tap_guard_until

    def _classify(self, start, jolt, jerk, rate, w_sum, n):
        self.last_event = start
        # With wrist-tap right click switched off, a hard finger press (big jolt, band barely
        # turning) is just a firm pinch - the rate/drift checks below still reject arm swings.
        if jolt >= self.s["tap_threshold"] and self.s["tap_enabled"]:
            kind = "tap"
        else:
            kind = "pinch" if jerk >= self.s["pinch_threshold"] else None
        # A pinch barely turns the band (measured <=0.32 rad/s), a tap <=1.9; turning the hand
        # over (e.g. into scroll posture) spins it much faster and must not click.
        if kind == "pinch" and rate > PINCH_ACCEPT_RATE or kind == "tap" and rate > TAP_MAX_RATE:
            kind = None
        # A pinch only shakes the band in place; the start or stop of a sweep also jerks, but
        # the band keeps turning one way.
        drift = _norm(_scale(w_sum, 1 / n))
        if kind == "pinch" and drift > PINCH_ACCEPT_DRIFT:
            kind = None
        if kind and start - self.last_tap < TAP_RINGING:
            kind = None  # the arm still ringing after a wrist tap (it can peak twice)
        elif kind == "tap":
            self.last_tap = start
        log(f"{kind or 'transient'} jolt={jolt:.2f} jerk={jerk:.3f} rate={rate:.2f} drift={drift:.2f}")
        if kind == "pinch":
            self.pinch_flash = time.monotonic()
        enabled = kind and (self.s[f"{kind}_enabled"] or kind == "pinch" and self.s["control_mode"] != "mouse")
        if not enabled:
            return
        if kind == "pinch" and start < self.settle_until:
            log("pinch ignored (settling after a toggle/click)")
            return  # the fingers letting go after the last series - must not start a new one
        if kind == "pinch":
            # Confirm before counting it: after a pinch the hand stays put, while the jerk at the
            # start of a sweep is followed by the hand moving on (see _gesture).
            self.tentative = {"start": start, "jerk": jerk, "until": start + EVENT_WINDOW + PINCH_CONFIRM,
                              "sum": 0.0, "n": 0}
            return
        self._count(kind, start, jerk)

    def _count(self, kind, start, jerk):
        # self.first[kind] = (time and jerk of the latest touch in the series, touches so far)
        last, last_jerk, count = self.first.get(kind, (None, 0.0, 0))
        gap_limit = TRIPLE_GAP if count >= 2 else TAP_MAX_GAP
        if last is None or not TAP_MIN_GAP < start - last < gap_limit:
            self.first = {kind: (start, jerk, 1)}  # a new series
            return
        # Compared with the touch just before: a release follows the pinch it belongs to
        if kind == "pinch" and jerk < RELEASE_RATIO * last_jerk:
            # Far weaker than the touch before it: the fingers letting go / shifting, not a pinch
            log(f"pinch release ignored ({jerk:.3f} vs {last_jerk:.3f})")
            return
        count += 1
        if kind == "tap":
            self.first = {}
            self._press(right=True)  # two wrist taps = right click
            return
        mouse = self.s["control_mode"] == "mouse"
        media = self.s["control_mode"] == "media"
        enabled = self.s["drag_scroll_enabled"] and mouse
        if count >= SCROLL_PINCHES and enabled:
            self.first, self.pending_click = {}, None
            self._set_scroll_mode(not self.dragging, start)  # four pinches: scroll mode (on/off)
            self.settle_until = start + SERIES_SETTLE
            return
        self.first = {kind: (start, jerk, count)}
        if count == 2:
            if self.dragging:
                return  # no clicking in scroll mode
            if enabled or media:
                # Wait to see whether more pinches follow (mouse: on the way to four; media:
                # three = play / pause) before acting on two
                self.pending_click = start + TRIPLE_GAP + EVENT_WINDOW + PINCH_CONFIRM
            else:
                self.first = {}
                if mouse:
                    self._press(right=False)  # two pinches = left click
                else:
                    self._command("pinch")
                self.settle_until = start + SERIES_SETTLE
        elif count == 3:
            self.pending_click = None  # three pinches: heading for four, not a click
            if media:
                self.first = {}
                self._command("pinch3")  # three pinches = play / pause
                self.settle_until = start + SERIES_SETTLE

    def laser_on(self):
        return self.s["control_mode"] == "slides" and self.s["laser_enabled"]

    def _swipe(self, sx, sy, ts, dt, t_play):
        """A quick, mostly straight stroke of the hand (sx > 0 = right, sy > 0 = down, rad/s)
        is a swipe, judged once the hand slows down again. Pointing-speed motion isn't."""
        if ts < self.swipe_guard:
            self.stroke = None  # the hand coming back after a swipe
            return
        speed = math.hypot(sx, sy)
        if speed > SWIPE_START:
            if self.stroke is None:
                self.stroke = [0.0, 0.0, 0.0, t_play - dt]
            st = self.stroke
            st[0], st[1], st[2] = st[0] + sx * dt, st[1] + sy * dt, max(st[2], speed)
            return
        if self.stroke is None:
            return
        dx, dy, peak, began = self.stroke
        self.stroke = None
        if peak < SWIPE_RATE or math.hypot(dx, dy) < SWIPE_ANGLE:
            return
        if abs(dx) >= SWIPE_RATIO * abs(dy):
            direction = "right" if dx > 0 else "left"
        elif abs(dy) >= SWIPE_RATIO * abs(dx):
            direction = "down" if dy > 0 else "up"
        else:
            log(f"swipe ignored: diagonal ({dx:.2f}, {dy:.2f})")
            return
        self.swipe_guard = ts + SWIPE_GUARD
        self.anchored, self.escape, self.first, self.pending_click = False, [], {}, None
        if self.laser_on():
            # The swipe's own motion, wind-up included, shouldn't move the red dot
            self.laser_rewind = began - SWIPE_WINDUP
        self._command(direction)

    def _command(self, gesture):
        """What a gesture does in presenter / media mode."""
        if self.paused:
            return
        mode = self.s["control_mode"]
        if mode == "slides":
            key = {"pinch": KEY_RIGHT, "right": KEY_RIGHT, "left": KEY_LEFT}.get(gesture)
            if key is not None:
                self._key(key)
                log(f"slides: {gesture} -> {'next' if key == KEY_RIGHT else 'previous'}")
        elif mode == "media":
            if gesture == "pinch":
                self._set_volume_mode(not self.volume_mode, time.monotonic())
                return
            keys = {"pinch3": [MEDIA_PLAY], "right": [MEDIA_NEXT], "left": [MEDIA_PREVIOUS],
                    "up": [MEDIA_SOUND_UP] * VOLUME_STEPS, "down": [MEDIA_SOUND_DOWN] * VOLUME_STEPS}[gesture]
            for k in keys:
                self._media_key(k)
            log(f"media: {gesture}")

    def _set_volume_mode(self, on, ts):
        self.volume_mode, self.volume_turn, self.volume_since = on, 0.0, ts
        self.stroke = None
        log(f"volume knob {'on' if on else 'off'}")

    def _volume_knob(self, roll, ts, dt):
        """Media mode, after two pinches: turning the wrist (roll about the forearm, which moves
        the band nowhere) steps the volume like a knob; the system's volume display shows it.
        Untouched for VOLUME_IDLE it switches itself off, so a forgotten knob can't surprise."""
        if ts - self.volume_since > VOLUME_IDLE:
            self._set_volume_mode(False, ts)
            return
        roll = _soft_deadzone(roll, VOLUME_DEAD) * (-1 if self.s["volume_invert"] else 1)
        self.volume_turn += roll * dt
        while abs(self.volume_turn) >= VOLUME_TURN:
            up = self.volume_turn > 0
            if not self.paused:
                self._media_key(MEDIA_SOUND_UP if up else MEDIA_SOUND_DOWN)
            self.volume_turn -= VOLUME_TURN if up else -VOLUME_TURN
            self.volume_since = ts

    def _key(self, code):
        for down in (True, False):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateKeyboardEvent(None, code, down))

    def _media_key(self, key):
        """Play/pause, next, volume... are not key codes but system-defined events (subtype 8),
        the way the keyboard's media row sends them."""
        for state in (0xA, 0xB):  # key down, key up
            ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                14, (0, 0), state << 8, 0, 0, None, 8, (key << 16) | (state << 8), -1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev.CGEvent())

    def _click(self, direction):
        self._press(right=True)  # twist = right click (left click is the double pinch)

    def _press(self, right):
        if self.paused:
            return
        right = right != self.s["swap_buttons"]
        Q = Quartz
        down, up, btn = ((Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp, Q.kCGMouseButtonRight) if right
                         else (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGMouseButtonLeft))
        now = time.monotonic()
        last_t, last_btn, count = self.last_click
        count = count + 1 if (last_btn == btn and now - last_t < 0.5) else 1
        self.last_click = (now, btn, count)
        pos = Q.CGEventGetLocation(Q.CGEventCreate(None))
        for kind in (down, up):
            ev = Q.CGEventCreateMouseEvent(None, kind, pos, btn)
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, count)
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
        log(f"click {'right' if right else 'left'} x{count}")

    def take_motion(self, now):
        """Sum of queued cursor motion that is due by now - self.delay. The delay is one
        batch plus jitter, so every sample is in hand by the time it is due."""
        q, due = self.queue, now - self.delay
        while q and q[0][0] < due - 0.5:  # stale (e.g. after a pause); drop rather than lurch
            q.popleft()
        dx = dy = sc = 0.0
        while q:
            end, a, b, span, s = q[0]
            if end <= due:
                q.popleft()
                dx, dy, sc = dx + a, dy + b, sc + s
                continue
            # Each sample's motion is spread evenly over its own interval, so ~100 samples/s
            # play out evenly at FRAME_RATE instead of 0, 1 or 2 samples landing per frame.
            start = end - span
            if start < due:
                f = (due - start) / span
                dx, dy, sc = dx + a * f, dy + b * f, sc + s * f
                q[0] = (end, a * (1 - f), b * (1 - f), end - due, s * (1 - f))
            break
        return dx, dy, sc

    async def run(self):
        Q = Quartz
        while True:
            dx, dy, sc = self.take_motion(time.monotonic())
            if sc and not self.paused:
                self.scroll_pending.append(sc)  # drag-scroll, already paced by the queue
            if self.paused:
                self.queue.clear()
            elif self.laser_on():
                self._move_laser(dx, dy, time.monotonic())
            elif dx or dy:
                pos = Q.CGEventGetLocation(Q.CGEventCreate(None))
                if self.cursor is None or abs(pos.x - self.cursor[0]) > 1.5 or abs(pos.y - self.cursor[1]) > 1.5:
                    self.cursor = (pos.x, pos.y)  # moved by something else (trackpad): resync
                b = Q.CGDisplayBounds(Q.CGMainDisplayID())
                # Keep our own sub-pixel position so slow, precise motion isn't rounded away.
                x = min(max(self.cursor[0] + dx, 0), b.size.width - 1)
                y = min(max(self.cursor[1] + dy, 0), b.size.height - 1)
                self.cursor = (x, y)
                ev = Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, (x, y), Q.kCGMouseButtonLeft)
                Q.CGEventPost(Q.kCGHIDEventTap, ev)
            if self.scroll_pending and not self.paused:
                amount = self.scroll_pending.pop(0) + self.scroll_rest
                whole = int(amount)
                self.scroll_rest = amount - whole
                if whole:
                    ev = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitPixel, 1, whole)
                    Q.CGEventPost(Q.kCGHIDEventTap, ev)
            await asyncio.sleep(1 / FRAME_RATE)


    def _move_laser(self, dx, dy, now):
        """Presenter mode: the hand's motion moves the red dot (drawn by LaserDot) instead of
        the cursor, anywhere on the connected displays (slides are often on a projector)."""
        Q = Quartz
        due = now - self.delay  # the play time of what take_motion just returned
        if self.laser_rewind is not None:
            t0, self.laser_rewind = self.laser_rewind, None
            self.queue = deque(e for e in self.queue if e[0] - e[3] < t0)  # the swipe's unplayed part
            back = [pos for t, pos in self.laser_hist if t <= t0]
            if back:
                self.laser = back[-1]
            dx = dy = 0.0
        if not (dx or dy):
            return
        if self.laser is None:
            pos = Q.CGEventGetLocation(Q.CGEventCreate(None))
            self.laser = (pos.x, pos.y)  # start where the mouse is
            self.laser_hist.append((due - 1.0, self.laser))
        if now - self.screens[0] > 2.0:
            err, ids, n = Q.CGGetActiveDisplayList(16, None, None)
            rects = [Q.CGDisplayBounds(i) for i in (ids or [])[:n]] or [Q.CGDisplayBounds(Q.CGMainDisplayID())]
            self.screens = (now, (min(r.origin.x for r in rects), min(r.origin.y for r in rects),
                                  max(r.origin.x + r.size.width for r in rects),
                                  max(r.origin.y + r.size.height for r in rects)))
        x0, y0, x1, y1 = self.screens[1]
        self.laser = (min(max(self.laser[0] + dx, x0), x1 - 1), min(max(self.laser[1] + dy, y0), y1 - 1))
        self.laser_moved = now
        self.laser_hist.append((due, self.laser))
        while self.laser_hist and self.laser_hist[0][0] < due - 2.0:
            self.laser_hist.popleft()


class Engine:
    """Owns the asyncio loop (background thread) that talks to the band."""

    def __init__(self, cfg, settings, on_status):
        self.cfg = cfg
        self.pointer = Pointer(settings)
        self.on_status = on_status
        self.loop = asyncio.new_event_loop()
        self.task = None
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def start(self):
        if not self.task or self.task.done():
            self.task = self.submit(self._run())

    def stop(self):
        if self.task:
            self.task.cancel()  # cancels _run; its finally closes the band cleanly

    async def _run(self):
        mover = asyncio.ensure_future(self.pointer.run())
        try:
            if self.pointer.s["device"] == "applewatch":
                await self._run_watch()
            else:
                await self._run_band()
        finally:
            mover.cancel()
            self.on_status("off", "Da tat")

    async def _run_watch(self):
        """Apple Watch: advertise as a Bluetooth peripheral and wait for the watch app to stream."""
        source = WatchSource(asyncio.get_running_loop(), self.pointer, log)
        source.start()
        self.on_status("connecting", "Dang cho Apple Watch (mo app AirMouse tren dong ho)...")
        streaming = False
        try:
            while True:
                if source.active() and not streaming:
                    streaming = True
                    self.pointer.rezero()
                    self.on_status("calibrating", "Giu yen tay...")
                    while self.pointer.bias is None and source.active():
                        await asyncio.sleep(0.2)
                    if source.active():
                        self.on_status("active", "Dang dieu khien chuot (Apple Watch)")
                elif streaming and not source.active():
                    streaming = False
                    self.on_status("connecting", "Mat tin hieu Apple Watch, dang cho...")
                await asyncio.sleep(0.3)
        finally:
            source.stop()

    async def _run_band(self):
        """Mi Band 10: connect, authenticate, keep the hidden workout's sensor stream going."""
        while True:
            band = Band(self.cfg["ble_address"], self.cfg["auth_key"], on_sensor=self.pointer, log=log,
                        name=self.cfg.get("name"))
            try:
                self.on_status("connecting", "Dang ket noi vong...")
                await band.connect()
                await band.start_sensor_stream()
                self.pointer.rezero()
                self.on_status("calibrating", "Giu yen tay...")
                while self.pointer.bias is None and not band.disconnected.is_set():
                    await asyncio.sleep(0.2)
                self.on_status("active", "Dang dieu khien chuot")
                await band.disconnected.wait()
                self.on_status("connecting", "Mat ket noi, dang thu lai...")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"error: {e}\n{traceback.format_exc()}")
                self.on_status("error", f"Loi: {e}")
            finally:
                try:
                    await asyncio.wait_for(band.close(), 3)
                except Exception:
                    pass
            await asyncio.sleep(5)

    def measure(self, seconds=2.0):
        """Sum gyro per axis over a window (blocking, called from the UI thread)."""
        p = self.pointer
        p.recording = {"raw": (0.0, 0.0, 0.0), "abs": (0.0, 0.0, 0.0), "yaw": 0.0, "pitch": 0.0,
                       "linear": (0.0, 0.0, 0.0), "n": 0}
        time.sleep(seconds)
        sums, p.recording = p.recording, None
        return sums

    def capture(self, seconds):
        """Record raw accel/gyro samples for a window (blocking, called from the UI thread)."""
        p = self.pointer
        p.capture = []
        time.sleep(seconds)
        samples, p.capture = p.capture, None
        return samples


class AirMouseApp(rumps.App):
    ICONS = {"off": "⌚︎", "connecting": "⌚︎…", "calibrating": "⌚︎◌", "active": "⌚︎●", "paused": "⌚︎‖", "error": "⌚︎!"}

    def __init__(self):
        super().__init__("AirMouse", title=self.ICONS["off"], quit_button=None)
        self.cfg, self.settings = load_config()
        self.state = "off"
        self.update = None  # newer release found on GitHub (see updates.py)
        self.update_auto = False  # it can be installed with one click (see installer.py)
        self.update_status = None  # progress / error text while installing it
        # The menu bar list is kept short on purpose: everything else is in the control window
        # (the Dock icon opens it).
        self.pause_item = rumps.MenuItem("Tạm dừng", callback=self.pause)
        self.menu = [self.pause_item, None, rumps.MenuItem("Thoát", callback=self.quit)]
        self.dirty = False  # settings changed in the window, saved by the next tick
        self._trusted, self._trust_checked = True, float("-inf")  # see trusted()
        self.engine = Engine(self.cfg, self.settings, self.set_status)
        self.updater = UpdateChecker(self._update_found, log, lambda: self.settings["update_check"])
        self.window_ctl = ControlWindow.alloc().initWithApp_(self)
        self.setup_ctl = SetupWindow.alloc().initWithApp_(self)
        self.refresh_checks()
        self.check_accessibility()
        self.updater.start()
        rumps.Timer(self.tick, 0.5).start()
        rumps.Timer(self.window_tick, 0.2).start()
        self.laser_dot = LaserDot()
        rumps.Timer(self.laser_tick, 1 / 60).start()
        self.window_ctl.show()
        if self.configured():
            self.toggle(None)  # start right away
        else:
            self.set_status("off", "Chua thiet lap vong")
            self.setup_ctl.show()  # first run: model, token, band

    # ---- device setup ----
    def configured(self):
        return bool(self.cfg.get("auth_key") and self.cfg.get("ble_address"))

    def open_setup(self, _):
        self.setup_ctl.show()

    def apply_setup(self, model, token, band):
        """Store what the setup window collected and (re)connect with it."""
        self.cfg.update(band_model=model, auth_key=token, ble_address=band["address"],
                        name=band.get("name") or self.cfg.get("name", ""))
        if band.get("mac"):
            self.cfg["mac"] = band["mac"]
        log(f"setup: {model} {band['address']} {band.get('mac')}")
        self.save()
        self.restart_engine()
        self.window_ctl.show()

    def restart_engine(self):
        """Stop the engine (closing the band's hidden workout) and start it again."""
        task = self.engine.task
        self.engine.stop()

        def again():
            if task is not None:
                try:
                    task.result(timeout=8)
                except BaseException:
                    pass  # cancelled - that's the point
            AppHelper.callAfter(self._start_engine)
        threading.Thread(target=again, daemon=True).start()

    def _start_engine(self):
        self.set_status("connecting", "Dang ket noi vong...")
        self.engine.start()

    def window_tick(self, _):
        if os.getppid() != PARENT:
            REQUESTS.add("quit")
        if "quit" in REQUESTS:
            REQUESTS.clear()
            self.quit(None)
            return
        if "show" in REQUESTS:
            REQUESTS.discard("show")
            self.window_ctl.show()
        if self.window_ctl.visible():
            self.window_ctl.refresh()

    def laser_tick(self, _):
        p = self.engine.pointer
        on = p.laser_on() and not p.paused and self.state == "active"
        self.laser_dot.update(p.laser if on else None, time.monotonic() - p.laser_moved, LASER_SHOW)

    def trusted(self):
        """Whether macOS lets us post mouse events. Without it they are dropped silently, so
        the band works but the cursor never moves. Rechecked every few seconds."""
        now = time.monotonic()
        if now - self._trust_checked > 3:
            self._trust_checked = now
            try:
                from ApplicationServices import AXIsProcessTrusted
                self._trusted = bool(AXIsProcessTrusted())
            except Exception:
                self._trusted = True
        return self._trusted

    def open_accessibility(self, _):
        os.system('open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"')

    def status_line(self):
        p = self.engine.pointer
        if not self.trusted():
            return "⚠  Chưa có quyền Trợ năng – chuột chưa di chuyển được"
        if self.state == "active":
            if p.paused:
                return "‖  Tạm dừng"
            mode = self.settings["control_mode"]
            if mode == "slides":
                return ("▶︎  Trình chiếu – tay chỉ chấm đỏ, cử chỉ điều khiển slide" if self.settings["laser_enabled"]
                        else "▶︎  Trình chiếu – con trỏ đứng yên, cử chỉ điều khiển slide")
            if mode == "media" and p.volume_mode:
                return "🔊  Chỉnh âm lượng – xoay cổ tay để tăng / giảm · chụm 2 lần để thoát"
            if mode == "media":
                return "♫  Media – con trỏ đứng yên, cử chỉ điều khiển nhạc / video"
            if p.palm_up:
                return "⇅  Chế độ cuộn trang (úp tay để quay lại)"
            if p.dragging:
                return "⇕  Chế độ cuộn: nâng tay để cuộn lên, hạ tay để cuộn xuống · vẩy tay nhanh để thoát"
            if p.anchored:
                return "⚓  Đang neo – chụm 2 lần để click"
            return "●  Đang điều khiển chuột"
        return {"off": "Đã tắt", "connecting": "…  Đang kết nối vòng",
                "calibrating": "◌  Giữ yên tay để đo điểm đứng yên"}.get(
                    self.state, "!  " + getattr(self, "status_text", ""))

    def detail_line(self):
        if not self.trusted():
            return "Bấm “Mở cài đặt Trợ năng”, bật MiBand AirMouse (nếu đã bật: tắt rồi bật lại)."
        if self.state == "error":
            return getattr(self, "status_text", "")
        s = self.settings  # list only the gestures that are switched on
        if s["control_mode"] == "slides":
            return (("Di tay: chấm đỏ · " if s["laser_enabled"] else "")
                    + "Chụm 2 lần hoặc lia nhanh sang phải: slide tiếp · lia sang trái: slide trước")
        if s["control_mode"] == "media":
            return ("Chụm 2 lần: núm âm lượng (xoay cổ tay) · chụm 3 lần: phát / dừng · "
                    "lia phải / trái: bài sau / trước · lia lên / xuống: âm lượng")
        parts = [("pinch_enabled", "Chụm 2 lần: click trái"),
                 ("drag_scroll_enabled", "Chụm 4 lần: chế độ cuộn (vẩy tay để thoát)"),
                 ("tap_enabled", "Gõ cổ tay 2 lần: click phải"),
                 ("click_enabled", "Vặn tay: click phải"),
                 ("scroll_enabled", "Ngửa tay + hất: cuộn")]
        return " · ".join(text for key, text in parts if s.get(key))

    # ---- status (may be called from the engine thread; the timer applies it) ----
    def set_status(self, state, text):
        self.state, self.status_text = state, text
        log(f"status {state}: {text}")

    def tick(self, _):
        if self.dirty:
            self.dirty = False
            self.save()
        state ="paused" if (self.engine.pointer.paused and self.state == "active") else self.state
        self.title = self.ICONS.get(state, "⌚︎")
        p = self.engine.pointer
        mode = self.settings["control_mode"]
        if state == "active" and mode == "media" and p.volume_mode:
            self.title = "⌚︎🔊"
        elif state == "active" and mode != "mouse":
            self.title = "⌚︎▶︎" if mode == "slides" else "⌚︎♫"
        elif state == "active" and p.palm_up:
            self.title = "⌚︎⇅"
        elif state == "active" and p.dragging:
            self.title = "⌚︎⇕"
        elif state == "active" and p.anchored:
            self.title = "⌚︎⚓"

    def refresh_checks(self):
        paused = self.engine.pointer.paused
        self.pause_item.title = "Tiếp tục" if paused else "Tạm dừng"

    def save(self):
        save_config(self.cfg, self.settings)
        self.refresh_checks()

    # ---- profiles (cfg["profiles"]: name -> settings; cfg["active_profile"]) ----
    def profile_names(self):
        return list(self.cfg["profiles"])

    def active_profile(self):
        return self.cfg.get("active_profile")

    def profile_dirty(self):
        """Current settings differ from the active profile (changed but not saved)."""
        saved = self.cfg["profiles"].get(self.active_profile())
        return saved is None or profile_of(self.settings) != saved

    def apply_profile(self, name):
        # Update in place: the pointer engine holds this very dict.
        self.settings.update(self.cfg["profiles"][name])
        self.cfg["active_profile"] = name
        self.engine.pointer.smoother.reset()
        log(f"profile applied: {name}")
        self.save()

    def profile_save(self, _):
        name = self.active_profile()
        if name not in self.cfg["profiles"]:
            return self.profile_save_as(None)
        self.cfg["profiles"][name] = profile_of(self.settings)
        log(f"profile saved: {name}")
        self.save()

    def profile_save_as(self, _):
        resp = rumps.Window("Đặt tên cho hồ sơ mới (lưu toàn bộ thông số đang dùng):",
                            "Lưu hồ sơ", default_text="", ok="Lưu", cancel="Huỷ",
                            dimensions=(260, 22)).run()
        name = resp.text.strip()
        if not resp.clicked or not name:
            return
        if name in self.cfg["profiles"] and not rumps.alert(
                "Ghi đè hồ sơ?", f"Đã có hồ sơ “{name}”. Ghi đè bằng thông số hiện tại?",
                ok="Ghi đè", cancel="Huỷ"):
            return
        self.cfg["profiles"][name] = profile_of(self.settings)
        self.cfg["active_profile"] = name
        log(f"profile saved as: {name}")
        self.save()

    def profile_delete(self, _):
        name = self.active_profile()
        if len(self.cfg["profiles"]) <= 1:
            rumps.alert("Không xoá được", "Cần giữ lại ít nhất một hồ sơ.")
            return
        if not rumps.alert("Xoá hồ sơ?", f"Xoá hồ sơ “{name}”? Thông số đang dùng vẫn giữ nguyên.",
                           ok="Xoá", cancel="Huỷ"):
            return
        self.cfg["profiles"].pop(name, None)
        self.cfg["active_profile"] = next(iter(self.cfg["profiles"]))
        log(f"profile deleted: {name}")
        self.save()

    def check_accessibility(self):
        try:
            from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
            if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}):
                rumps.alert("Can quyen Tro nang (Accessibility)",
                            "De di chuyen chuot, hay bat 'MiBand AirMouse' trong System Settings > "
                            "Privacy & Security > Accessibility, roi mo lai ung dung.")
        except Exception as e:
            log(f"accessibility check failed: {e}")

    # ---- menu actions ----
    def toggle(self, _):
        if not self.configured():
            self.setup_ctl.show()
            return
        if self.state == "off" or not self.engine.task or self.engine.task.done():
            self.set_status("connecting", "Dang ket noi vong...")
            self.engine.start()
        else:
            self.engine.stop()

    def pause(self, _):
        self.engine.pointer.paused = not self.engine.pointer.paused
        self.refresh_checks()

    def rezero(self, _):
        self.engine.pointer.rezero()
        self.set_status("calibrating", "Giu yen tay...")
        threading.Thread(target=self._wait_rezero, daemon=True).start()

    def _wait_rezero(self):
        while self.engine.pointer.bias is None and self.state == "calibrating":
            time.sleep(0.2)
        if self.state == "calibrating":
            self.set_status("active", "Dang dieu khien chuot")

    # ---- new releases (the window shows a notice; nothing is installed automatically) ----
    def _update_found(self, info):
        # From the checker's thread (codesign runs here, not in the window's refresh)
        reason = installer.can_install(info)
        if reason:
            log(f"update {info['version']}: one-click install not possible ({reason})")
        self.update_auto, self.update = reason is None, info

    def update_available(self):
        info = self.update
        return info if info and info["version"] != self.settings["skip_version"] else None

    def open_update(self, _):
        info = self.update
        if not info or self.update_status and not self.update_status.startswith("Lỗi"):
            return  # nothing to do, or already installing
        if not self.update_auto:
            subprocess.run(["open", info["page"]])
            return
        self.update_status = "Đang tải…"
        threading.Thread(target=self._install_update, args=(info,), daemon=True).start()

    def _install_update(self, info):
        """Background: download and check; then (main thread) swap and quit, the helper reopens."""
        def progress(fraction, text):
            self.update_status = text
        try:
            new = installer.prepare(info, progress)
        except Exception as e:
            log(f"update {info['version']} failed: {type(e).__name__}: {e}")
            self.update_status, self.update_auto = f"Lỗi: {e}", False  # the button now opens the page
            return
        log(f"update {info['version']} verified, installing")
        self.update_status = "Đang cài…"
        AppHelper.callAfter(self._swap_and_quit, new)

    def _swap_and_quit(self, new):
        installer.install(new, PARENT, LOG)  # waits for the Dock launcher to exit
        self.quit(None)

    def skip_update(self, _):
        if self.update:
            self.settings["skip_version"] = self.update["version"]
            log(f"update {self.update['version']} skipped")
            self.save()

    def set_control(self, mode):
        """Mouse, presenter or media control (also from the window's picker)."""
        self.settings["control_mode"] = mode
        p = self.engine.pointer
        p.stroke, p.pending_click, p.first = None, None, {}
        log(f"control mode: {mode}")
        self.save()

    def calibrate(self, _):
        if self.state != "active":
            rumps.alert("Chua san sang", "Hay doi den khi bieu tuong hien ⌚︎● (dang dieu khien chuot).")
            return
        p = self.engine.pointer
        p.paused = True
        try:
            rumps.alert("Hieu chinh 1/3", "Dat tay o tu the ban hay dung.\n\n"
                        "Bam OK, roi VAN CANG TAY qua lai vai lan (nhu van nam cua) trong 2 giay.")
            twist = self.engine.measure()
            rumps.alert("Hieu chinh 2/3", "Bam OK, roi DUA VONG SANG PHAI trong 2 giay - "
                        "theo kieu ban se dung (lia ca tay hay gac khuyu tay deu duoc).")
            right = self.engine.measure()
            rumps.alert("Hieu chinh 3/3", "Bam OK, roi DUA VONG LEN TREN trong 2 giay.")
            up = self.engine.measure()
            r, u = right["raw"], up["raw"]

            # Raw-axes mode (old behaviour)
            ax = max(range(3), key=lambda i: abs(r[i]))
            ay = max((i for i in range(3) if i != ax), key=lambda i: abs(u[i]))
            # Plane mode: twisting the forearm spins the band around exactly the forearm axis.
            # Twisting back and forth cancels in the sum, so compare the summed magnitudes.
            fa = max(range(3), key=lambda i: twist["abs"][i])
            side = _hand_rates(r, p.grav, fa)[0]
            down = _hand_rates(u, p.grav, fa)[1]
            # Sweeping the arm pulls the band toward the elbow (centripetal), which tells us
            # which way along the forearm axis points to the hand.
            n = (right["n"] + up["n"]) or 1
            toward_elbow = (right["linear"][fa] + up["linear"][fa]) / n
            if abs(toward_elbow) > 0.3:
                self.settings["hand_dir"] = 1 if toward_elbow < 0 else -1
            self.settings.update({"axis_x": "xyz"[ax], "invert_x": r[ax] < 0,
                                  "axis_y": "xyz"[ay], "invert_y": not (u[ay] < 0),
                                  "forearm_axis": "xyz"[fa], "invert_yaw": side < 0,
                                  "invert_pitch": down > 0, "mode": "plane",
                                  "plane_calibrated": True})
            palm = p._palm_dir()  # the posture used for calibration is the normal (palm-down) one
            if palm:
                self.settings["palm_ref"] = list(palm)
            self.save()
            log(f"calibrated twist={twist} right={right} up={up} grav={p.grav} -> {self.settings}")
            rumps.alert("Xong", f"Da hieu chinh. Cang tay nam doc truc {'xyz'[fa]} cua vong; "
                        f"van cang tay quanh truc do de click.")
        finally:
            p.paused = False
            self.refresh_checks()

    def pinch_test(self, _):
        """Guided recording to see whether a thumb-index pinch is visible in the band's data.
        Writes pinch_test.csv (phase, t, ax, ay, az, gx, gy, gz) for analysis."""
        if self.state != "active":
            rumps.alert("Chua san sang", "Hay doi den khi bieu tuong hien ⌚︎● hoac ⌚︎⚓.")
            return
        steps = [
            ("rest", 6, "Buoc 1/3 - NEN",
             "Dat tay o tu the dung chuot, de YEN HOAN TOAN.\n\nBam OK roi giu yen 6 giay."),
            ("pinch", 14, "Buoc 2/3 - CHUM NGON",
             "Giu tay yen, chi CHAM NGON CAI VAO NGON TRO 10 LAN, moi lan cach khoang 1 giay "
             "(cham dut khoat, roi mo ra).\n\nBam OK roi bat dau ngay. Co 14 giay."),
            ("tap", 8, "Buoc 3/3 - GO CO TAY",
             "De so sanh: GO CO TAY RA TRUOC 5 LAN, moi lan cach khoang 1 giay.\n\n"
             "Bam OK roi bat dau ngay. Co 8 giay."),
        ]
        p = self.engine.pointer
        p.paused = True  # no cursor motion or clicks while recording
        path = os.path.join(DATA, "pinch_test.csv")
        try:
            rows = []
            for phase, seconds, title, text in steps:
                rumps.alert(title, text)
                rows += [(phase,) + s for s in self.engine.capture(seconds)]
            with private_open(path, "w") as f:
                f.write("phase,t,ax,ay,az,gx,gy,gz\n")
                for r in rows:
                    f.write(r[0] + "," + ",".join(f"{v:.5f}" for v in r[1:]) + "\n")
            log(f"pinch test saved {len(rows)} samples to {path}")
            rumps.alert("Xong", f"Da ghi {len(rows)} mau vao pinch_test.csv.\nBao Claude de phan tich.")
        finally:
            p.paused = False
            self.refresh_checks()

    def quit(self, _):
        self.engine.stop()
        time.sleep(1.5)  # let the band close its hidden workout
        rumps.quit_application()


if __name__ == "__main__":
    # The Dock launcher (launcher.m) asks via signals: SIGUSR1 = show the window, SIGTERM =
    # quit cleanly. Handlers only queue a request; window_tick acts on it on the main thread.
    signal.signal(signal.SIGUSR1, lambda *_: REQUESTS.add("show"))
    signal.signal(signal.SIGTERM, lambda *_: REQUESTS.add("quit"))
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGUSR1})  # the launcher starts us with it blocked
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    AirMouseApp().run()
