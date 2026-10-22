"""Draws the app icon (a band + pointer) and writes AppIcon.icns next to this file."""
import os
import shutil
import subprocess
import sys

import AppKit as A

HERE = os.path.dirname(os.path.abspath(__file__))


def draw(size):
    img = A.NSImage.alloc().initWithSize_((size, size))
    img.lockFocus()
    s = size / 1024.0
    # macOS icon grid: rounded square inset ~10%
    rect = A.NSMakeRect(100 * s, 100 * s, 824 * s, 824 * s)
    path = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, 185 * s, 185 * s)
    grad = A.NSGradient.alloc().initWithStartingColor_endingColor_(
        A.NSColor.colorWithSRGBRed_green_blue_alpha_(0.16, 0.45, 0.98, 1),
        A.NSColor.colorWithSRGBRed_green_blue_alpha_(0.42, 0.20, 0.86, 1))
    grad.drawInBezierPath_angle_(path, -60)

    white = A.NSColor.whiteColor()
    # the band: a capsule strap with a rounded screen
    white.colorWithAlphaComponent_(0.35).setFill()
    A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        A.NSMakeRect(420 * s, 170 * s, 184 * s, 684 * s), 92 * s, 92 * s).fill()
    white.setFill()
    A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        A.NSMakeRect(392 * s, 352 * s, 240 * s, 320 * s), 96 * s, 96 * s).fill()

    # motion arcs on both sides of the band
    white.setStroke()
    for side in (-1, 1):
        for r in (210, 290):
            arc = A.NSBezierPath.bezierPath()
            arc.setLineWidth_(30 * s)
            arc.setLineCapStyle_(A.NSLineCapStyleRound)
            start, end = (150, 210) if side < 0 else (-30, 30)
            arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_((512 * s, 512 * s), r * s, start, end)
            arc.stroke()

    # a pointer arrow on the band's screen
    arrow = A.NSBezierPath.bezierPath()
    pts = [(470, 620), (470, 430), (515, 475), (545, 410), (575, 425), (545, 488), (605, 488)]
    arrow.moveToPoint_((pts[0][0] * s, pts[0][1] * s))
    for x, y in pts[1:]:
        arrow.lineToPoint_((x * s, y * s))
    arrow.closePath()
    A.NSColor.colorWithSRGBRed_green_blue_alpha_(0.20, 0.30, 0.85, 1).setFill()
    arrow.fill()
    img.unlockFocus()
    rep = A.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
    return rep.representationUsingType_properties_(A.NSBitmapImageFileTypePNG, {})


def main(out_dir):
    iconset = os.path.join(out_dir, "AppIcon.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset)
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = base * scale
            name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
            draw(px).writeToFile_atomically_(os.path.join(iconset, name), True)
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(out_dir, "AppIcon.icns")], check=True)
    draw(512).writeToFile_atomically_(os.path.join(out_dir, "AppIcon-preview.png"), True)
    shutil.rmtree(iconset)


if __name__ == "__main__":
    A.NSApplication.sharedApplication()
    main(sys.argv[1] if len(sys.argv) > 1 else HERE)
