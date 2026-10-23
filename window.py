"""Control window for MiBand AirMouse: live meters, sliders and switches for every setting.

Minimizing or closing the window just hides it - the app keeps running in the menu bar.
"""
import AppKit as A
import objc
from Foundation import NSObject

CW = 430         # column width
W = 2 * CW       # window width
PAD = 18
ROW = 30
LABEL_W = 150
VALUE_W = 70

# key, label, min, max, value format
SLIDERS = [
    ("speed", "Tốc độ", 150, 3000, "{:.0f}"),
    ("accel", "Tăng tốc khi lia nhanh", 0, 10, "{:.1f}"),
    ("deadzone", "Vùng chết", 0, 0.15, "{:.2f}"),
    ("pinch_threshold", "Ngưỡng chụm ngón", 0.04, 0.25, "{:.2f}"),
    ("tap_threshold", "Ngưỡng gõ cổ tay", 1.8, 3.2, "{:.1f}"),
    ("click_threshold", "Ngưỡng vặn tay", 2.0, 8.0, "{:.1f}"),
    ("scroll_speed", "Tốc độ cuộn", 400, 4000, "{:.0f}"),
]
INT_KEYS = {"speed", "scroll_speed"}

CHECKS = [
    ("anchor_enabled", "Neo chuột khi dừng tay 1 giây"),
    ("pinch_enabled", "Click trái: chụm ngón cái + trỏ 2 lần"),
    ("tap_enabled", "Click phải: gõ cổ tay 2 lần"),
    ("click_enabled", "Click phải: vặn cẳng tay"),
    ("scroll_enabled", "Cuộn trang: ngửa tay rồi hất lên"),
    ("scroll_invert", "Đảo chiều cuộn"),
    ("drag_scroll_enabled", "Chế độ cuộn: chụm 4 lần, vẩy tay nhanh để thoát"),
    ("ignore_push", "Bỏ qua đưa tay tới / lùi"),
    ("flip_x", "Đảo chiều trái / phải"),
    ("flip_y", "Đảo chiều lên / xuống"),
]

MODES = [("plane", "Theo mặt phẳng"), ("axes", "Theo trục cảm biến")]
CONTROL_MODES = [("mouse", "Chuột", "cursorarrow"), ("slides", "Trình chiếu", "play.rectangle"),
                 ("media", "Media", "music.note")]  # key, label, SF Symbol
SMOOTH_NAMES = [("Tat", "Tắt"), ("Vua", "Vừa"), ("Manh", "Mạnh")]


class AirMouseWindow(A.NSWindow):
    """The yellow button hides to the menu bar instead of the Dock."""

    def miniaturize_(self, sender):
        self.delegate().hide()

    def performMiniaturize_(self, sender):
        self.delegate().hide()


class ControlWindow(NSObject):
    def initWithApp_(self, app):
        self = objc.super(ControlWindow, self).init()
        if self is None:
            return None
        self.app = app
        self.sliders = {}   # key -> (slider, value label, fmt)
        self.checks = {}    # key -> checkbox
        self.tag_keys = {}  # control tag -> key / action
        self._build()
        return self

    # ---------- layout helpers (y counts down from the top) ----------
    @objc.python_method
    def _label(self, text, x, y, w, size=12, bold=False, color=None):
        f = A.NSTextField.labelWithString_(text)
        f.setFrame_(A.NSMakeRect(x, self.h - y - 18, w, 18))
        f.setFont_(A.NSFont.boldSystemFontOfSize_(size) if bold else A.NSFont.systemFontOfSize_(size))
        if color:
            f.setTextColor_(color)
        self.view.addSubview_(f)
        return f

    @objc.python_method
    def _section(self, title):
        self.y += 10
        self._label(title, self.x0 + PAD, self.y, self.cw - 2 * PAD, 13, bold=True)
        self.y += 24

    @objc.python_method
    def _tag(self, key):
        tag = len(self.tag_keys) + 1
        self.tag_keys[tag] = key
        return tag

    @objc.python_method
    def _slider(self, key, text, lo, hi, fmt):
        self._label(text, self.x0 + PAD, self.y + 4, LABEL_W)
        s = A.NSSlider.sliderWithValue_minValue_maxValue_target_action_(lo, lo, hi, self, "sliderChanged:")
        s.setFrame_(A.NSMakeRect(self.x0 + PAD + LABEL_W, self.h - self.y - 24, self.cw - 2 * PAD - LABEL_W - VALUE_W, 24))
        s.setTag_(self._tag(key))
        self.view.addSubview_(s)
        v = self._label("", self.x0 + self.cw - PAD - VALUE_W + 8, self.y + 4, VALUE_W - 8)
        v.setAlignment_(A.NSTextAlignmentRight)
        self.sliders[key] = (s, v, fmt)
        self.y += ROW

    @objc.python_method
    def _check(self, key, text):
        c = A.NSButton.checkboxWithTitle_target_action_(text, self, "checkChanged:")
        c.setFrame_(A.NSMakeRect(self.x0 + PAD, self.h - self.y - 22, self.cw - 2 * PAD, 22))
        c.setTag_(self._tag(key))
        self.view.addSubview_(c)
        self.checks[key] = c
        self.y += 26

    @objc.python_method
    def _segments(self, key, items):
        seg = A.NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            [label for _, label in items], A.NSSegmentSwitchTrackingSelectOne, self, "segmentChanged:")
        seg.setFrame_(A.NSMakeRect(self.x0 + PAD + LABEL_W, self.h - self.y - 24, self.cw - 2 * PAD - LABEL_W, 24))
        seg.setTag_(self._tag(key))
        self.view.addSubview_(seg)
        return seg

    @objc.python_method
    def _meter(self, text, maximum):
        self._label(text, self.x0 + PAD, self.y + 2, LABEL_W)
        m = A.NSLevelIndicator.alloc().initWithFrame_(
            A.NSMakeRect(self.x0 + PAD + LABEL_W, self.h - self.y - 18, self.cw - 2 * PAD - LABEL_W - VALUE_W, 16))
        m.setLevelIndicatorStyle_(A.NSLevelIndicatorStyleContinuousCapacity)
        m.setMinValue_(0)
        m.setMaxValue_(maximum)
        self.view.addSubview_(m)
        v = self._label("", self.x0 + self.cw - PAD - VALUE_W + 8, self.y + 2, VALUE_W - 8, 11)
        v.setAlignment_(A.NSTextAlignmentRight)
        self.y += 24
        return m, v

    @objc.python_method
    def _buttons(self, items):
        n = len(items)
        bw = (self.cw - 2 * PAD - (n - 1) * 8) / n
        for i, (title, action) in enumerate(items):
            b = A.NSButton.buttonWithTitle_target_action_(title, self, "buttonClicked:")
            b.setFrame_(A.NSMakeRect(self.x0 + PAD + i * (bw + 8), self.h - self.y - 30, bw, 30))
            b.setTag_(self._tag(action))
            self.view.addSubview_(b)
            self.buttons[action] = b
        self.y += 36

    @objc.python_method
    def _profile_row(self):
        """Hồ sơ: [profile popup] • chưa lưu   [Lưu] [Lưu thành…] [Xoá]"""
        self.y += 4
        self._label("Hồ sơ", PAD, self.y + 5, 50, 13, bold=True)
        self.profile_popup = A.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            A.NSMakeRect(PAD + 54, self.h - self.y - 28, 250, 28), False)
        self.profile_popup.setTarget_(self)
        self.profile_popup.setAction_("profileSelected:")
        self.view.addSubview_(self.profile_popup)
        self.profile_dirty = self._label("", PAD + 312, self.y + 5, 110, 11,
                                         color=A.NSColor.systemOrangeColor())
        x = PAD + 430
        bw = (W - PAD - x - 2 * 8) / 3
        for i, (title, action) in enumerate((("Lưu", "profile_save"), ("Lưu thành…", "profile_save_as"),
                                             ("Xoá hồ sơ", "profile_delete"))):
            b = A.NSButton.buttonWithTitle_target_action_(title, self, "buttonClicked:")
            b.setFrame_(A.NSMakeRect(x + i * (bw + 8), self.h - self.y - 30, bw, 30))
            b.setTag_(self._tag(action))
            self.view.addSubview_(b)
            self.buttons[action] = b
        self.y += 40

    @objc.python_method
    def _build(self):
        self.h = 1040
        self.y = PAD
        self.buttons = {}
        style = (A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable
                 | A.NSWindowStyleMaskMiniaturizable | A.NSWindowStyleMaskFullSizeContentView)
        self.window = AirMouseWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            A.NSMakeRect(0, 0, W, self.h), style, A.NSBackingStoreBuffered, False)
        self.window.setTitle_("MiBand AirMouse")
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        self.view = A.NSVisualEffectView.alloc().initWithFrame_(A.NSMakeRect(0, 0, W, self.h))
        self.view.setMaterial_(A.NSVisualEffectMaterialSidebar)
        self.view.setBlendingMode_(A.NSVisualEffectBlendingModeBehindWindow)
        self.view.setState_(A.NSVisualEffectStateActive)
        self.window.setContentView_(self.view)

        self.x0, self.cw = 0, W  # header spans both columns
        self.y += 22  # title bar
        self.status = self._label("Đang khởi động...", PAD, self.y, W - 2 * PAD, 15, bold=True)
        self.y += 22
        self.detail = self._label("", PAD, self.y, W - 2 * PAD, 11, color=A.NSColor.secondaryLabelColor())
        self.y += 26
        self._buttons([("Tắt chuột bay", "toggle"), ("Tạm dừng", "pause"), ("Thu nhỏ xuống thanh menu", "hide"),
                       ("Thoát ứng dụng", "quit")])
        self._profile_row()
        self._label("Điều khiển", PAD, self.y + 5, 80, 13, bold=True)
        self.seg_control = A.NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            [label for _, label, _ in CONTROL_MODES], A.NSSegmentSwitchTrackingSelectOne, self, "controlChanged:")
        for i, (_, label, symbol) in enumerate(CONTROL_MODES):
            self.seg_control.setImage_forSegment_(
                A.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, label), i)
        self.seg_control.setFrame_(A.NSMakeRect(PAD + 84, self.h - self.y - 28, 420, 28))
        self.view.addSubview_(self.seg_control)
        # A newer release on GitHub: shown in the free space beside the mode picker
        self.update_label = self._label("", PAD + 520, self.y + 6, 150, 13, bold=True,
                                        color=A.NSColor.systemBlueColor())
        for title, action, x, w in (("Tải về…", "open_update", PAD + 672, 90), ("Bỏ qua", "skip_update", PAD + 766, 58)):
            b = A.NSButton.buttonWithTitle_target_action_(title, self, "buttonClicked:")
            b.setFrame_(A.NSMakeRect(x, self.h - self.y - 30, w, 30))
            b.setTag_(self._tag(action))
            self.view.addSubview_(b)
            self.buttons[action] = b
        self.buttons["open_update"].setKeyEquivalent_("")
        self.buttons["open_update"].setBezelColor_(A.NSColor.systemBlueColor())
        self.y += 38
        top = self.y

        self.x0, self.cw = 0, CW  # left column: what the hand does, and how it moves the cursor
        self._section("Đo trực tiếp")
        self.m_motion, self.v_motion = self._meter("Tốc độ tay", 2.0)
        self.m_pinch, self.v_pinch = self._meter("Lực chụm ngón", 0.5)
        self.m_tap, self.v_tap = self._meter("Lực gõ cổ tay", 6.0)
        self.m_twist, self.v_twist = self._meter("Lực vặn tay", 10.0)
        self._section("Di chuyển")
        self._label("Chế độ", self.x0 + PAD, self.y + 4, LABEL_W)
        self.seg_mode = self._segments("mode", MODES)
        self.y += ROW
        self._label("Độ mượt (khử run tay)", self.x0 + PAD, self.y + 4, LABEL_W)
        self.seg_smooth = self._segments("smoothing", SMOOTH_NAMES)
        self.y += ROW
        for key, text, lo, hi, fmt in SLIDERS[:3]:
            self._slider(key, text, lo, hi, fmt)
        for key, text in CHECKS[7:]:
            self._check(key, text)
        left_end = self.y

        self.x0, self.cw, self.y = CW, CW, top  # right column: clicks, scrolling, tools
        self._section("Click")
        for key, text in CHECKS[:4]:
            self._check(key, text)
        for key, text, lo, hi, fmt in SLIDERS[3:6]:
            self._slider(key, text, lo, hi, fmt)
        self._section("Cuộn trang")
        for key, text in CHECKS[4:7]:
            self._check(key, text)
        self._slider(*SLIDERS[6])
        self._section("Công cụ")
        self._buttons([("Hiệu chỉnh hướng…", "calibrate"), ("Đặt lại điểm đứng yên", "rezero")])
        self._buttons([("Đo thử chụm ngón…", "pinch_test"), ("Mở cài đặt Trợ năng", "open_accessibility")])
        self._buttons([("Thiết lập thiết bị…", "open_setup")])
        self.y = max(left_end, self.y)

        # Shrink the window to what was laid out (controls were placed from the top)
        used = self.y + PAD
        shift = self.h - used
        for v in self.view.subviews():
            f = v.frame()
            v.setFrameOrigin_((f.origin.x, f.origin.y - shift))
        self.h = used
        self.window.setContentSize_((W, used))
        self.view.setFrameSize_((W, used))
        self.window.center()
        self.refresh(force=True)

    # ---------- show / hide ----------
    @objc.python_method
    def show(self):
        A.NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    @objc.python_method
    def hide(self):
        self.window.orderOut_(None)

    @objc.python_method
    def visible(self):
        return self.window.isVisible()

    def windowShouldClose_(self, sender):
        self.hide()
        return False

    # ---------- settings <-> controls ----------
    @objc.python_method
    def _get(self, key):
        s = self.app.settings
        if key == "flip_x":
            return s["invert_yaw" if s["mode"] == "plane" else "invert_x"]
        if key == "flip_y":
            return s["invert_pitch" if s["mode"] == "plane" else "invert_y"]
        return s[key]

    @objc.python_method
    def _set(self, key, value):
        s = self.app.settings
        if key == "flip_x":
            key = "invert_yaw" if s["mode"] == "plane" else "invert_x"
        elif key == "flip_y":
            key = "invert_pitch" if s["mode"] == "plane" else "invert_y"
        s[key] = value
        self.app.dirty = True

    def sliderChanged_(self, sender):
        key = self.tag_keys[sender.tag()]
        v = sender.doubleValue()
        v = int(round(v)) if key in INT_KEYS else round(v, 3)
        self._set(key, v)
        self.sliders[key][1].setStringValue_(self.sliders[key][2].format(v))

    def checkChanged_(self, sender):
        self._set(self.tag_keys[sender.tag()], bool(sender.state()))

    def profileSelected_(self, sender):
        self.app.apply_profile(sender.titleOfSelectedItem())
        self.refresh(force=True)

    def controlChanged_(self, sender):
        self.app.set_control(CONTROL_MODES[sender.selectedSegment()][0])
        self.refresh(force=True)

    def segmentChanged_(self, sender):
        key = self.tag_keys[sender.tag()]
        items = MODES if key == "mode" else SMOOTH_NAMES
        self._set(key, items[sender.selectedSegment()][0])
        self.refresh(force=True)  # flip_x / flip_y now refer to the other mode's settings

    def buttonClicked_(self, sender):
        action = self.tag_keys[sender.tag()]
        if action == "hide":
            self.hide()
        else:
            getattr(self.app, action)(None)
        self.refresh(force=True)

    @objc.python_method
    def refresh(self, force=False):
        """Pull live values and settings into the controls (also picks up menu changes).
        Settings aren't pushed while a mouse button is down, so a slider being dragged
        doesn't fight the user."""
        app, p = self.app, self.app.engine.pointer
        self.status.setStringValue_(app.status_line())
        self.status.setTextColor_(A.NSColor.systemRedColor() if not app.trusted() else A.NSColor.labelColor())
        self.detail.setStringValue_(app.detail_line())
        self.buttons["toggle"].setTitle_("Tắt chuột bay" if app.state != "off" else "Bật chuột bay")
        self.buttons["pause"].setTitle_("Tiếp tục" if p.paused else "Tạm dừng")
        names = app.profile_names()
        if list(self.profile_popup.itemTitles()) != names:
            self.profile_popup.removeAllItems()
            self.profile_popup.addItemsWithTitles_(names)
        if app.active_profile() in names:
            self.profile_popup.selectItemWithTitle_(app.active_profile())
        self.profile_dirty.setStringValue_("• chưa lưu" if app.profile_dirty() else "")
        info = app.update_available()
        self.update_label.setStringValue_(f"Có bản mới {info['version']}" if info else "")
        for v in (self.update_label, self.buttons["open_update"], self.buttons["skip_update"]):
            v.setHidden_(info is None)

        motion = (p.motion[0] ** 2 + p.motion[1] ** 2) ** 0.5
        s = app.settings
        for meter, label, value, thr in (
                (self.m_motion, self.v_motion, motion, None),
                (self.m_pinch, self.v_pinch, p.live_pinch, s["pinch_threshold"]),
                (self.m_tap, self.v_tap, p.live_tap, s["tap_threshold"]),
                (self.m_twist, self.v_twist, p.last_peak, s["click_threshold"])):
            meter.setDoubleValue_(min(value, meter.maxValue()))
            if thr is not None:
                meter.setWarningValue_(thr)
                meter.setCriticalValue_(meter.maxValue() * 2)  # warning colour = above threshold
                label.setStringValue_(f"{value:.2f}/{thr:g}")
            else:
                label.setStringValue_(f"{value:.2f}")

        if not force and A.NSEvent.pressedMouseButtons():
            return
        for key, (slider, label, fmt) in self.sliders.items():
            slider.setDoubleValue_(s[key])
            label.setStringValue_(fmt.format(s[key]))
        for key, box in self.checks.items():
            box.setState_(1 if self._get(key) else 0)
        self.seg_mode.setSelectedSegment_([k for k, _ in MODES].index(s["mode"]))
        self.seg_control.setSelectedSegment_([m[0] for m in CONTROL_MODES].index(s["control_mode"]))
        self.seg_smooth.setSelectedSegment_([k for k, _ in SMOOTH_NAMES].index(s["smoothing"]))
