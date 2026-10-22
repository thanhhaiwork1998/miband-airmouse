"""First-run setup: choose the band model, paste its token (auth key), pick the band nearby.

Shown when config.json has no band yet, and from "Thiết lập thiết bị…" any time.
"""
import os
import re
import subprocess
import sys
import threading

import AppKit as A
import objc
from Foundation import NSObject
from PyObjCTools import AppHelper

import discovery
import xiaomi_cloud

W, PAD, LABEL_W = 600, 22, 150
KNOWN_MODELS = {"miwear.watch.o66gl": "Xiaomi Smart Band 10"}  # cloud model id -> MODELS label
EXTRACTOR = os.path.expanduser("~/xiaomi-extractor/token_extractor.py")
EXTRACTOR_REPO = "https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor"

# (label, supported) - only Xiaomi's newer BLE protocol (fe95 / 005e-005f) is implemented
MODELS = [
    ("Xiaomi Smart Band 10", True),
    ("Xiaomi Smart Band 9 / 9 Pro / 9 Active (thử nghiệm)", True),
    ("Xiaomi Smart Band 8 (chưa hỗ trợ)", False),
    ("Mi Band 4 – 7 (chưa hỗ trợ)", False),
    ("Mi Band 2 – 3 (chưa hỗ trợ)", False),
]


def clean_token(text):
    """Accepts '0x…', spaces and upper case; returns 32 lowercase hex chars or None."""
    t = re.sub(r"\s+", "", text or "").lower()
    if t.startswith("0x"):
        t = t[2:]
    return t if re.fullmatch(r"[0-9a-f]{32}", t) else None


class SetupWindow(NSObject):
    def initWithApp_(self, app):
        self = objc.super(SetupWindow, self).init()
        if self is None:
            return None
        self.app = app
        self.bands = []
        self._build()
        self.qr = QrWindow.alloc().initWithSetup_(self)
        return self

    # ---------- layout (y counts down from the top) ----------
    @objc.python_method
    def _label(self, text, x, y, w, size=13, bold=False, color=None, h=18):
        f = A.NSTextField.wrappingLabelWithString_(text)
        f.setFrame_(A.NSMakeRect(x, self.h - y - h, w, h))
        f.setFont_(A.NSFont.boldSystemFontOfSize_(size) if bold else A.NSFont.systemFontOfSize_(size))
        if color:
            f.setTextColor_(color)
        self.view.addSubview_(f)
        return f

    @objc.python_method
    def _button(self, title, action, x, y, w, primary=False):
        b = A.NSButton.buttonWithTitle_target_action_(title, self, action)
        b.setFrame_(A.NSMakeRect(x, self.h - y - 30, w, 30))
        if primary:
            b.setKeyEquivalent_("\r")
        self.view.addSubview_(b)
        return b

    @objc.python_method
    def _build(self):
        self.h = 400
        style = A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable
        self.window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            A.NSMakeRect(0, 0, W, self.h), style, A.NSBackingStoreBuffered, False)
        self.window.setTitle_("Thiết lập thiết bị")
        self.window.setReleasedWhenClosed_(False)
        self.view = A.NSVisualEffectView.alloc().initWithFrame_(A.NSMakeRect(0, 0, W, self.h))
        self.view.setMaterial_(A.NSVisualEffectMaterialSidebar)  # same look as the control window
        self.view.setBlendingMode_(A.NSVisualEffectBlendingModeBehindWindow)
        self.view.setState_(A.NSVisualEffectStateActive)
        self.window.setContentView_(self.view)
        x2, w2 = PAD + LABEL_W, W - 2 * PAD - LABEL_W
        y = PAD

        self._label("Kết nối vòng tay Mi Band", PAD, y, W - 2 * PAD, 18, bold=True, h=24)
        y += 30
        self._label("Chọn đời vòng, dán token (auth key) của vòng, rồi chọn đúng vòng ở gần Mac.",
                    PAD, y, W - 2 * PAD, 12, color=A.NSColor.secondaryLabelColor())
        y += 34

        # 1. Model
        self._label("1. Đời vòng", PAD, y + 5, LABEL_W, bold=True)
        self.model_popup = A.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            A.NSMakeRect(x2, self.h - y - 28, w2, 28), False)
        self.model_popup.setAutoenablesItems_(False)
        for label, supported in MODELS:
            self.model_popup.addItemWithTitle_(label)
            self.model_popup.lastItem().setEnabled_(supported)
        self.view.addSubview_(self.model_popup)
        y += 46

        # 2. Token
        self._label("2. Token", PAD, y + 5, LABEL_W, bold=True)
        self.token_field = A.NSTextField.alloc().initWithFrame_(A.NSMakeRect(x2, self.h - y - 26, w2 - 130, 26))
        self.token_field.setPlaceholderString_("32 ký tự 0–9, a–f (mục TOKEN)")
        self.token_field.setFont_(A.NSFont.monospacedSystemFontOfSize_weight_(13, 0))
        self.view.addSubview_(self.token_field)
        self._button("Quét mã QR…", "getToken:", W - PAD - 122, y - 2, 122)
        y += 32
        self._label("Bấm “Quét mã QR…” rồi quét bằng app Mi Fitness hoặc Xiaomi Home (tài khoản đã "
                    "ghép vòng). Token sẽ được điền tự động.",
                    x2, y, w2, 11, color=A.NSColor.secondaryLabelColor(), h=30)
        y += 46

        # 3. Band
        self._label("3. Chọn vòng", PAD, y + 5, LABEL_W, bold=True)
        self.band_popup = A.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            A.NSMakeRect(x2, self.h - y - 28, w2 - 130, 28), False)
        self.view.addSubview_(self.band_popup)
        self.scan_button = self._button("Tìm vòng", "scan:", W - PAD - 122, y - 1, 122)
        y += 34
        self.scan_status = self._label("Bật sáng màn hình vòng và tắt Bluetooth điện thoại, rồi bấm “Tìm vòng”.",
                                       x2, y, w2, 11, color=A.NSColor.secondaryLabelColor(), h=30)
        y += 50

        self.error = self._label("", PAD, y, W - 2 * PAD, 12, color=A.NSColor.systemRedColor(), h=34)
        y += 40
        self.cancel_button = self._button("Huỷ", "cancel:", W - PAD - 320, y, 150)
        self._button("Lưu và kết nối", "save:", W - PAD - 160, y, 160, primary=True)
        self.window.center()

    # ---------- show / fill ----------
    @objc.python_method
    def show(self):
        cfg = self.app.cfg
        self.token_field.setStringValue_(cfg.get("auth_key", ""))
        model = cfg.get("band_model")
        if model and self.model_popup.itemWithTitle_(model):
            self.model_popup.selectItemWithTitle_(model)
        self.bands = []
        if cfg.get("ble_address"):
            self.bands.append({"address": cfg["ble_address"], "name": cfg.get("name", "Vòng đang dùng"),
                               "mac": cfg.get("mac"), "rssi": None, "connected": True})
        self._fill_bands()
        self.error.setStringValue_("")
        self.cancel_button.setEnabled_(self.app.configured())
        A.NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        if not self.bands:
            self.scan_(None)

    @objc.python_method
    def _fill_bands(self):
        self.band_popup.removeAllItems()
        if not self.bands:
            self.band_popup.addItemWithTitle_("(chưa tìm thấy vòng nào)")
            return
        for b in self.bands:
            self.band_popup.addItemWithTitle_(discovery.describe(b) if b["rssi"] is not None
                                              else f"{b['name']} · đang dùng")

    # ---------- actions ----------
    def getToken_(self, sender):
        self.qr.show()

    @objc.python_method
    def open_password_tool(self):
        """Fallback: the original extractor in Terminal (password login)."""
        if os.path.exists(EXTRACTOR):
            python = sys.executable  # the venv, or the runtime bundled in the prebuilt app
            cmd = f"'{python}' '{EXTRACTOR}'"  # single quotes inside AppleScript's double-quoted string
            subprocess.run(["osascript", "-e", 'tell application "Terminal" to activate',
                            "-e", f'tell application "Terminal" to do script "{cmd}"'])
        else:
            subprocess.run(["open", EXTRACTOR_REPO])

    def scan_(self, sender):
        self.scan_button.setEnabled_(False)
        self.scan_status.setStringValue_("Đang tìm vòng (khoảng 8 giây)…")
        future = self.app.engine.submit(discovery.find_bands())

        def wait():
            try:
                found = future.result(timeout=20)
                AppHelper.callAfter(self._scan_done, found, None)
            except Exception as e:
                AppHelper.callAfter(self._scan_done, [], e)
        threading.Thread(target=wait, daemon=True).start()

    @objc.python_method
    def _scan_done(self, found, error):
        self.scan_button.setEnabled_(True)
        current = {b["address"] for b in self.bands}
        self.bands += [b for b in found if b["address"] not in current]
        self._fill_bands()
        if error is not None:
            self.scan_status.setStringValue_(f"Không tìm được: {error}")
        elif not found:
            self.scan_status.setStringValue_("Chưa thấy vòng nào. Bật sáng màn hình vòng, tắt Bluetooth "
                                             "điện thoại rồi bấm “Tìm vòng” lại.")
        else:
            self.scan_status.setStringValue_(f"Tìm thấy {len(found)} vòng. Đối chiếu địa chỉ MAC với kết "
                                             "quả lấy token nếu có nhiều vòng.")

    def cancel_(self, sender):
        self.window.orderOut_(None)

    @objc.python_method
    def use_cloud_band(self, band):
        """A band picked from the Xiaomi account: fill its token, its model if known, and select
        it in the band list when its MAC matches one found nearby."""
        self.token_field.setStringValue_(band["token"])
        model = KNOWN_MODELS.get(band["model"])
        if model:
            self.model_popup.selectItemWithTitle_(model)
        for i, b in enumerate(self.bands):
            if band["mac"] and (b.get("mac") or "").upper() == band["mac"]:
                self.band_popup.selectItemAtIndex_(i)
                self.error.setStringValue_("")
                return
        if band["mac"]:
            self.scan_status.setStringValue_(f"Đã điền token của {band['name']} ({band['mac']}). "
                                             "Chọn đúng vòng có địa chỉ này trong danh sách.")

    def save_(self, sender):
        token = clean_token(self.token_field.stringValue())
        model_item = self.model_popup.selectedItem()
        index = self.band_popup.indexOfSelectedItem()
        if model_item is None or not model_item.isEnabled():
            return self.error.setStringValue_("Đời vòng này chưa được hỗ trợ.")
        if token is None:
            return self.error.setStringValue_("Token chưa đúng: cần đúng 32 ký tự 0–9 hoặc a–f "
                                              "(chấp nhận cả dạng có 0x ở đầu).")
        if not self.bands or index < 0:
            return self.error.setStringValue_("Chưa chọn vòng. Bấm “Tìm vòng” để tìm vòng ở gần.")
        band = self.bands[index]
        self.app.apply_setup(model=model_item.title(), token=token, band=band)
        self.window.orderOut_(None)


class QrWindow(NSObject):
    """Xiaomi login by QR code: shows the code, waits for the phone to approve, then lists the
    bands on the account so their token can be filled in."""

    def initWithSetup_(self, setup):
        self = objc.super(QrWindow, self).init()
        if self is None:
            return None
        self.setup = setup
        self.cancelled = threading.Event()
        self.devices = []
        self._build()
        return self

    @objc.python_method
    def _build(self):
        w, h = 380, 520
        self.h = h
        self.window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            A.NSMakeRect(0, 0, w, h), A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable,
            A.NSBackingStoreBuffered, False)
        self.window.setTitle_("Lấy token bằng mã QR")
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        view = A.NSVisualEffectView.alloc().initWithFrame_(A.NSMakeRect(0, 0, w, h))
        view.setMaterial_(A.NSVisualEffectMaterialSidebar)
        view.setBlendingMode_(A.NSVisualEffectBlendingModeBehindWindow)
        view.setState_(A.NSVisualEffectStateActive)
        self.window.setContentView_(view)

        def label(text, y, height, size=12, bold=False, secondary=False):
            f = A.NSTextField.wrappingLabelWithString_(text)
            f.setFrame_(A.NSMakeRect(PAD, h - y - height, w - 2 * PAD, height))
            f.setAlignment_(A.NSTextAlignmentCenter)
            f.setFont_(A.NSFont.boldSystemFontOfSize_(size) if bold else A.NSFont.systemFontOfSize_(size))
            if secondary:
                f.setTextColor_(A.NSColor.secondaryLabelColor())
            view.addSubview_(f)
            return f

        label("Quét mã bằng Mi Fitness hoặc Xiaomi Home", 18, 22, 15, bold=True)
        label("Mở app trên điện thoại (đăng nhập tài khoản đã ghép vòng) → biểu tượng quét mã → xác nhận đăng nhập.",
              44, 34, 11, secondary=True)
        self.image = A.NSImageView.alloc().initWithFrame_(A.NSMakeRect((w - 260) / 2, h - 88 - 260, 260, 260))
        self.image.setImageScaling_(A.NSImageScaleProportionallyUpOrDown)
        self.image.setWantsLayer_(True)
        self.image.layer().setBackgroundColor_(A.NSColor.whiteColor().CGColor())
        self.image.layer().setCornerRadius_(10)
        view.addSubview_(self.image)
        self.status = label("", 360, 36, 12)
        self.picker = A.NSPopUpButton.alloc().initWithFrame_pullsDown_(A.NSMakeRect(PAD, h - 400 - 28, w - 2 * PAD, 28), False)
        self.picker.setHidden_(True)
        view.addSubview_(self.picker)

        def button(title, action, x, y, bw):
            b = A.NSButton.buttonWithTitle_target_action_(title, self, action)
            b.setFrame_(A.NSMakeRect(x, h - y - 30, bw, 30))
            view.addSubview_(b)
            return b

        self.retry = button("Tạo mã mới", "retry:", PAD, 440, 160)
        self.use = button("Dùng vòng này", "use:", w - PAD - 160, 440, 160)
        self.use.setKeyEquivalent_("\r")
        self.password = button("Đăng nhập bằng mật khẩu (Terminal)…", "password:", PAD, 478, w - 2 * PAD)
        self.password.setBordered_(False)
        self.password.setContentTintColor_(A.NSColor.linkColor())
        self.window.center()

    # ---------- flow ----------
    @objc.python_method
    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        self._begin()

    @objc.python_method
    def _begin(self):
        self.cancelled = threading.Event()  # one per attempt: an older attempt keeps its own
        self.devices = []
        self.picker.setHidden_(True)
        self.use.setEnabled_(False)
        self.retry.setEnabled_(False)
        self.image.setImage_(None)
        self.status.setStringValue_("Đang lấy mã QR từ Xiaomi…")
        threading.Thread(target=self._run, args=(self.cancelled,), daemon=True).start()

    @objc.python_method
    def _run(self, cancelled):
        login = xiaomi_cloud.QrLogin()
        try:
            png = login.start()
        except Exception as e:
            return AppHelper.callAfter(self._fail, f"Không lấy được mã QR: {e}")
        if cancelled.is_set():
            return
        AppHelper.callAfter(self._show_qr, png)
        try:
            ok = login.wait(cancelled.is_set)
        except Exception as e:
            return AppHelper.callAfter(self._fail, f"Lỗi khi chờ đăng nhập: {e}")
        if cancelled.is_set():
            return  # superseded by "Tạo mã mới" or the window was closed
        if not ok:
            return AppHelper.callAfter(self._fail, "Mã QR đã hết hạn. Bấm “Tạo mã mới”.")
        AppHelper.callAfter(self.status.setStringValue_, "Đăng nhập thành công. Đang tìm vòng trong tài khoản…")
        try:
            devices = login.wearables()
        except Exception as e:
            return AppHelper.callAfter(self._fail, f"Không đọc được danh sách thiết bị: {e}")
        AppHelper.callAfter(self._got_devices, devices)

    @objc.python_method
    def _show_qr(self, png):
        data = A.NSData.dataWithBytes_length_(png, len(png))
        self.image.setImage_(A.NSImage.alloc().initWithData_(data))
        self.status.setStringValue_("Đang chờ bạn quét mã và xác nhận trên điện thoại… (mã hết hạn sau 5 phút)")

    @objc.python_method
    def _fail(self, message):
        self.status.setStringValue_(message)
        self.retry.setEnabled_(True)

    @objc.python_method
    def _got_devices(self, devices):
        self.retry.setEnabled_(True)
        self.devices = devices
        if not devices:
            self.status.setStringValue_("Tài khoản này không có vòng Xiaomi nào. Kiểm tra lại tài khoản "
                                        "đã ghép vòng trong Mi Fitness.")
            return
        if len(devices) == 1:
            self.setup.use_cloud_band(devices[0])
            self.window.orderOut_(None)
            return
        self.picker.removeAllItems()
        for d in devices:
            self.picker.addItemWithTitle_(f"{d['name']} · {d['mac'] or d['model']}")
        nearby = {(b.get("mac") or "").upper() for b in self.setup.bands}
        for i, d in enumerate(devices):
            if d["mac"] in nearby:
                self.picker.selectItemAtIndex_(i)  # the one that is actually near the Mac
        self.picker.setHidden_(False)
        self.use.setEnabled_(True)
        self.status.setStringValue_(f"Tài khoản có {len(devices)} vòng. Chọn vòng muốn dùng.")

    # ---------- actions ----------
    def retry_(self, sender):
        self.cancelled.set()
        self._begin()

    def use_(self, sender):
        i = self.picker.indexOfSelectedItem()
        if 0 <= i < len(self.devices):
            self.setup.use_cloud_band(self.devices[i])
            self.window.orderOut_(None)

    def password_(self, sender):
        self.cancelled.set()
        self.window.orderOut_(None)
        self.setup.open_password_tool()

    def windowWillClose_(self, notification):
        self.cancelled.set()  # stop waiting for a login nobody will finish
