"""The presenter's red dot: a tiny transparent window that floats above everything (full-screen
Keynote / PowerPoint included), ignores the mouse, and follows Pointer.laser."""
import AppKit as A
import Quartz

SIZE = 44     # window side, pt (room for the glow)
DOT = 18      # dot diameter, pt
FADE = 0.5    # s the dot takes to fade out


class LaserDot:
    def __init__(self):
        w = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            A.NSMakeRect(0, 0, SIZE, SIZE), A.NSWindowStyleMaskBorderless, A.NSBackingStoreBuffered, False)
        w.setOpaque_(False)
        w.setBackgroundColor_(A.NSColor.clearColor())
        w.setHasShadow_(False)
        w.setIgnoresMouseEvents_(True)
        w.setLevel_(A.NSScreenSaverWindowLevel)
        w.setCollectionBehavior_(A.NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | A.NSWindowCollectionBehaviorStationary
                                 | A.NSWindowCollectionBehaviorFullScreenAuxiliary
                                 | A.NSWindowCollectionBehaviorIgnoresCycle)
        view = w.contentView()
        view.setWantsLayer_(True)
        dot = Quartz.CALayer.layer()
        dot.setFrame_((((SIZE - DOT) / 2, (SIZE - DOT) / 2), (DOT, DOT)))
        dot.setCornerRadius_(DOT / 2)
        red = A.NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.13, 0.1, 1.0).CGColor()
        dot.setBackgroundColor_(red)
        dot.setBorderColor_(A.NSColor.colorWithWhite_alpha_(1.0, 0.85).CGColor())
        dot.setBorderWidth_(1.5)
        dot.setShadowColor_(red)
        dot.setShadowOpacity_(0.9)
        dot.setShadowRadius_(8)
        dot.setShadowOffset_((0, 0))
        view.layer().addSublayer_(dot)
        self.window, self.shown = w, False

    def update(self, pos, moved_ago, show_for):
        """pos: global screen point (top-left origin, as Quartz events use) or None."""
        if pos is None or moved_ago > show_for + FADE:
            if self.shown:
                self.window.orderOut_(None)
                self.shown = False
            return
        # Cocoa puts the origin at the bottom-left of the main screen
        main_h = A.NSScreen.screens()[0].frame().size.height
        self.window.setFrameOrigin_((pos[0] - SIZE / 2, main_h - pos[1] - SIZE / 2))
        self.window.setAlphaValue_(1.0 if moved_ago <= show_for else 1.0 - (moved_ago - show_for) / FADE)
        if not self.shown:
            self.window.orderFrontRegardless()
            self.shown = True
