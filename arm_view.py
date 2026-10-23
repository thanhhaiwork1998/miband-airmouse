"""Live picture of the arm for the control window: the forearm's height seen from the side,
its left/right direction seen from above, the wrist's turn seen from the front, and where the
hand is moving (with anchor / pinch)."""
import math
import time

import AppKit as A
import objc

AXIS = {"x": 0, "y": 1, "z": 2}
FPS = 30
FLASH = 0.5  # s a felt pinch lights the motion ring


def _dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def _cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def arm_state(p):
    """Angles the view draws, from the pointer's gravity estimate (drift-free):
    elevation: forearm above horizontal, rad (+ = hand up, in the calibrated screen sense)
    roll: wrist turn from the normal pointing posture, rad (None until known)
    start: forearm elevation when scroll mode began (None outside scroll mode)."""
    grav, s = getattr(p, "grav", None), getattr(p, "s", {})
    if grav is None:
        return None
    f = AXIS.get(s.get("forearm_axis", "x"), 0)
    k = -1 if s.get("invert_pitch") else 1  # the same sign the scroll joystick uses
    elevation = math.asin(max(-1.0, min(1.0, grav[f] * k)))
    ref = getattr(p, "joy_ref", None)
    start = math.asin(max(-1.0, min(1.0, ref * k))) if getattr(p, "dragging", False) and ref is not None else None
    roll = None
    palm_ref = s.get("palm_ref")
    across = list(grav)
    across[f] = 0.0
    n = math.sqrt(_dot(across, across))
    if palm_ref and n > 0.5:  # the forearm pointing steeply up/down can't tell the palm's side
        d = [v / n for v in across]
        roll = math.atan2(_cross(palm_ref, d)[f], _dot(palm_ref, d))
    return elevation, roll, start


class ArmView(A.NSView):
    def initWithFrame_source_(self, frame, source):
        self = objc.super(ArmView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.source = source  # () -> the pointer (or None)
        self.setToolTip_("Bấm vào để lấy hướng trái / phải hiện tại làm giữa")
        A.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / FPS, self, "tick:", None, True)
        return self

    def tick_(self, timer):
        if self.window() is not None and self.window().isVisible():
            self.setNeedsDisplay_(True)

    def isFlipped(self):
        return False

    def mouseDown_(self, event):
        p = self.source()
        if p is not None:
            p.yaw = 0.0  # left/right has no absolute reference: the user says where the middle is

    # ---------- drawing helpers ----------
    @objc.python_method
    def _text(self, text, x, y, size=10, color=None, bold=False, center=True):
        attrs = {A.NSFontAttributeName: A.NSFont.boldSystemFontOfSize_(size) if bold else A.NSFont.systemFontOfSize_(size),
                 A.NSForegroundColorAttributeName: color or A.NSColor.secondaryLabelColor()}
        s = A.NSString.stringWithString_(text)
        w = s.sizeWithAttributes_(attrs).width
        s.drawAtPoint_withAttributes_((x - w / 2 if center else x, y), attrs)

    @objc.python_method
    def _line(self, a, b, color, width=2.0, dash=None):
        path = A.NSBezierPath.bezierPath()
        path.moveToPoint_(a)
        path.lineToPoint_(b)
        path.setLineWidth_(width)
        path.setLineCapStyle_(A.NSLineCapStyleRound)
        if dash:
            path.setLineDash_count_phase_(dash, len(dash), 0)
        color.setStroke()
        path.stroke()

    @objc.python_method
    def _circle(self, c, r, stroke=None, fill=None, width=1.5):
        path = A.NSBezierPath.bezierPathWithOvalInRect_(((c[0] - r, c[1] - r), (2 * r, 2 * r)))
        if fill:
            fill.setFill()
            path.fill()
        if stroke:
            path.setLineWidth_(width)
            stroke.setStroke()
            path.stroke()

    # ---------- the three panels ----------
    def drawRect_(self, rect):
        p = self.source()
        w, h = self.bounds().size.width, self.bounds().size.height
        q = w / 4
        state = arm_state(p) if p is not None else None
        if state is None:
            self._text("Chờ dữ liệu từ vòng… (giữ yên tay)", w / 2, h / 2 - 6, 11)
            return
        elevation, roll, start = state
        self._side(q / 2, h, elevation, start)
        self._top(q * 1.5, h, getattr(p, "yaw", 0.0))
        self._wrist(q * 2.5, h, roll, p)
        self._motion(q * 3.5, h, p)

    @objc.python_method
    def _side(self, cx, h, elevation, start):
        """Side view: shoulder, upper arm hanging to the elbow, forearm at its real height."""
        ink, accent, faint = A.NSColor.labelColor(), A.NSColor.systemBlueColor(), A.NSColor.tertiaryLabelColor()
        elbow = (cx - 20, 60)
        shoulder = (cx - 38, 100)
        length = 52
        self._line((elbow[0], elbow[1]), (elbow[0] + length + 6, elbow[1]), faint, 1, dash=[3, 3])  # horizontal
        if start is not None:  # scroll mode's starting height
            a = max(-0.95, min(0.95, start))
            self._line(elbow, (elbow[0] + length * math.cos(a), elbow[1] + length * math.sin(a)),
                       A.NSColor.systemOrangeColor(), 1.5, dash=[4, 3])
        a = max(-0.95, min(0.95, elevation))
        hand = (elbow[0] + length * math.cos(a), elbow[1] + length * math.sin(a))
        band = (elbow[0] + 0.78 * length * math.cos(a), elbow[1] + 0.78 * length * math.sin(a))
        self._circle(shoulder, 6, fill=faint)
        self._line(shoulder, elbow, faint, 6)
        self._line(elbow, hand, ink, 6)
        self._circle(elbow, 4, fill=ink)
        self._circle(hand, 7, fill=ink)
        self._circle(band, 5, fill=accent)  # the band on the wrist
        self._text(f"Nâng / hạ  {math.degrees(elevation):+.0f}°", cx, 4)

    @objc.python_method
    def _top(self, cx, h, yaw):
        """Seen from above: shoulders, and the forearm turned left / right from the middle."""
        ink, accent, faint = A.NSColor.labelColor(), A.NSColor.systemBlueColor(), A.NSColor.tertiaryLabelColor()
        elbow = (cx, 34)
        length = 56
        self._line((cx - 30, 24), (cx + 30, 24), faint, 7)  # shoulders
        self._line(elbow, (cx, elbow[1] + length + 6), faint, 1, dash=[3, 3])  # straight ahead
        a = max(-1.3, min(1.3, yaw))
        hand = (cx + length * math.sin(a), elbow[1] + length * math.cos(a))
        band = (cx + 0.78 * length * math.sin(a), elbow[1] + 0.78 * length * math.cos(a))
        self._line(elbow, hand, ink, 6)
        self._circle(elbow, 4, fill=ink)
        self._circle(hand, 7, fill=ink)
        self._circle(band, 5, fill=accent)
        self._text(f"Trái / phải  {math.degrees(yaw):+.0f}°", cx, 4)

    @objc.python_method
    def _wrist(self, cx, h, roll, p):
        """Front view of the wrist: where the palm faces, turned from the normal posture."""
        c, r = (cx, 64), 30
        knob = getattr(p, "volume_mode", False)
        palm_up = getattr(p, "palm_up", False)
        ring = A.NSColor.systemOrangeColor() if knob else A.NSColor.tertiaryLabelColor()
        self._circle(c, r, stroke=ring, width=3 if knob else 1.5)
        if roll is None:
            self._text("Xoay  –", cx, 4)
            return
        a = -math.pi / 2 + roll  # palm down = needle down
        ux, uy = math.cos(a), math.sin(a)
        # the forearm's cross-section: a flat oval across the needle
        path = A.NSBezierPath.bezierPathWithOvalInRect_(((-17, -8), (34, 16)))
        t = A.NSAffineTransform.transform()
        t.translateXBy_yBy_(c[0], c[1])
        t.rotateByRadians_(a + math.pi / 2)
        path.transformUsingAffineTransform_(t)
        A.NSColor.secondaryLabelColor().setFill()
        path.fill()
        palm = A.NSColor.systemGreenColor() if palm_up else A.NSColor.systemBlueColor()
        self._line(c, (c[0] + ux * (r - 5), c[1] + uy * (r - 5)), palm, 3)
        self._circle((c[0] + ux * (r - 5), c[1] + uy * (r - 5)), 4, fill=palm)
        label = "Núm âm lượng" if knob else ("Ngửa tay  " if palm_up else "Xoay  ") + f"{math.degrees(roll):+.0f}°"
        self._text(label, cx, 4, color=A.NSColor.systemOrangeColor() if knob else None)

    @objc.python_method
    def _motion(self, cx, h, p):
        """Where the hand is moving (dot), anchor and scroll state, and a flash on each pinch."""
        c, r = (cx, 64), 30
        flash = time.monotonic() - getattr(p, "pinch_flash", 0.0)
        flashing = 0.0 <= flash < FLASH
        anchored, dragging = getattr(p, "anchored", False), getattr(p, "dragging", False)
        base = A.NSColor.systemGreenColor() if anchored else A.NSColor.tertiaryLabelColor()
        self._circle(c, r, stroke=base, width=2.5 if anchored else 1.5)
        if flashing:
            k = 1 - flash / FLASH
            self._circle(c, r + 3, stroke=A.NSColor.systemOrangeColor().colorWithAlphaComponent_(k), width=5 * k + 1)
        faint = A.NSColor.quaternaryLabelColor()
        self._line((c[0] - r + 6, c[1]), (c[0] + r - 6, c[1]), faint, 1)
        self._line((c[0], c[1] - r + 6), (c[0], c[1] + r - 6), faint, 1)
        sx, sy = getattr(p, "live_motion", (0.0, 0.0))
        speed = math.hypot(sx, sy)
        k = min(1.0, speed / 1.5) / speed if speed > 1e-6 else 0.0  # 1.5 rad/s reaches the rim
        dot = (c[0] + sx * k * (r - 6), c[1] - sy * k * (r - 6))  # screen down = view down
        self._line(c, dot, A.NSColor.systemBlueColor(), 2)
        self._circle(dot, 5, fill=A.NSColor.systemBlueColor())
        if flashing:
            label, color = "Chụm!", A.NSColor.systemOrangeColor()
        elif dragging:
            label, color = "Đang cuộn", None
        elif anchored:
            label, color = "Đang neo", A.NSColor.systemGreenColor()
        else:
            label, color = ("Đang di chuyển" if speed > 0.08 else "Đứng yên"), None
        self._text(label, cx, 4, color=color)
