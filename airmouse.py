"""Mi Band 10 air mouse for macOS.

  python airmouse.py --probe   # only print sensor data (to check axes and rate)
  python airmouse.py           # move the cursor with the band's gyro

Ctrl+C to stop.
"""
import argparse
import asyncio
import json
import math
import os
import time

from band import Band

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


class Probe:
    def __init__(self):
        self.batches = 0
        self.samples = 0
        self.t0 = time.monotonic()

    def __call__(self, batch):
        self.batches += 1
        self.samples += len(batch.gyro)
        elapsed = max(time.monotonic() - self.t0, 1e-6)
        a = batch.accel[-1] if batch.accel else None
        g = batch.gyro[-1] if batch.gyro else None
        fmt = lambda s: f"({s.x:7.2f},{s.y:7.2f},{s.z:7.2f})" if s else "(none)"
        print(f"batch#{self.batches:4} accel={len(batch.accel):2} gyro={len(batch.gyro):2}  "
              f"a={fmt(a)} g={fmt(g)}  ~{self.samples / elapsed:5.1f} mau/giay")


class Calibrator:
    """Guided: user turns the wrist right, then tilts up; the dominant gyro axis and its sign win."""

    def __init__(self):
        self.sums = None

    def __call__(self, batch):
        if self.sums is not None:
            for s in batch.gyro:
                self.sums[0] += s.x
                self.sums[1] += s.y
                self.sums[2] += s.z

    async def measure(self, prompt, exclude=None):
        print(f"\n>>> {prompt}")
        for i in (3, 2, 1):
            print(f"    chuan bi... {i}")
            await asyncio.sleep(1)
        print("    BAT DAU! (lam trong 2 giay)")
        self.sums = [0.0, 0.0, 0.0]
        await asyncio.sleep(2)
        sums, self.sums = self.sums, None
        axis = max((i for i in range(3) if "xyz"[i] != exclude), key=lambda i: abs(sums[i]))
        print(f"    tong gyro x={sums[0]:.1f} y={sums[1]:.1f} z={sums[2]:.1f} -> truc {'xyz'[axis]}")
        return "xyz"[axis], sums[axis] < 0

    async def run(self, cfg):
        print("Gio thang canh tay ra truoc nhu cam but laser chi vao man hinh, giu yen.")
        print("Vong o cang tay nen chi gap ban tay se KHONG co tac dung - hay di chuyen ca cang tay, khong van tay.")
        await asyncio.sleep(2)
        ax, inv_x = await self.measure("LIA CA CANG TAY SANG PHAI (nhu chi laser tu trai sang phai), roi giu yen")
        await asyncio.sleep(1)
        ay, up_neg = await self.measure("NANG CA CANG TAY LEN (nhu chi laser tu duoi len tren), roi giu yen", exclude=ax)
        # screen y grows downward, so "up" must produce negative dy
        cfg["mouse"] = {"axis_x": ax, "invert_x": inv_x, "axis_y": ay, "invert_y": not up_neg}
        with open(CONFIG, "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"\nDa luu cau hinh chuot: {cfg['mouse']}")
        print("Gio chay: ../venv/bin/python airmouse.py")


class Mouse:
    """Gyro -> cursor. Batches arrive in bursts, so movement is spread over the batch interval."""

    def __init__(self, args):
        import Quartz
        self.Q = Quartz
        self.args = args
        self.bias = None
        self.calib = []
        self.pending = []  # (dx, dy) still to apply
        self.last_batch = None
        self.interval = 0.1

    def __call__(self, batch):
        now = time.monotonic()
        if self.last_batch:
            self.interval = 0.8 * self.interval + 0.2 * min(now - self.last_batch, 1.0)
        self.last_batch = now
        gyro = [(s.x, s.y, s.z) for s in batch.gyro]
        if not gyro:
            return
        if self.bias is None:  # first ~2s: hold still, measure gyro drift
            self.calib += gyro
            if len(self.calib) >= 20 and now - self.calib_start > 2:
                n = len(self.calib)
                self.bias = tuple(sum(v[i] for v in self.calib) / n for i in range(3))
                print(f"Hieu chinh xong (bias={tuple(round(b, 3) for b in self.bias)}). Bat dau dieu khien chuot!")
            return
        axis = {"x": 0, "y": 1, "z": 2}
        dt = self.interval / len(gyro)
        dx = dy = 0.0
        for v in gyro:
            gx = v[axis[self.args.axis_x]] - self.bias[axis[self.args.axis_x]]
            gy = v[axis[self.args.axis_y]] - self.bias[axis[self.args.axis_y]]
            gx = 0 if abs(gx) < self.args.deadzone else gx
            gy = 0 if abs(gy) < self.args.deadzone else gy
            dx += gx * dt * self.args.speed * (-1 if self.args.invert_x else 1)
            dy += gy * dt * self.args.speed * (-1 if self.args.invert_y else 1)
        steps = 8
        self.pending = [(dx / steps, dy / steps)] * steps

    async def run(self):
        self.calib_start = time.monotonic()
        print("Giu yen tay khoang 2 giay de hieu chinh...")
        Q = self.Q
        while True:
            if self.pending:
                dx, dy = self.pending.pop(0)
                pos = Q.CGEventGetLocation(Q.CGEventCreate(None))
                bounds = Q.CGDisplayBounds(Q.CGMainDisplayID())
                x = min(max(pos.x + dx, 0), bounds.size.width - 1)
                y = min(max(pos.y + dy, 0), bounds.size.height - 1)
                ev = Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, (x, y), Q.kCGMouseButtonLeft)
                Q.CGEventPost(Q.kCGHIDEventTap, ev)
            await asyncio.sleep(max(self.interval / 8, 0.005))


DIAG_COMPANIONS = [
    (0, "MacBook", 34),    # what Gadgetbridge sends (Android)
    (1, "iPhone", 17),
    (2, "iPhone", 17),
    (1, "iPhone15,2", 17),
]


async def diagnose(cfg):
    """Try each companion identity; report which one the band keeps connected after auth."""
    results = []
    for comp in DIAG_COMPANIONS:
        print(f"\n=== Thu khai bao {comp} ===")
        band = Band(cfg["ble_address"], cfg["auth_key"], debug=True, companion=comp)
        kept = False
        try:
            await band.connect()
            print("  Xac thuc OK, cho 4 giay...")
            await asyncio.sleep(4)
            kept = band.client.is_connected
            if kept:
                band.request_device_info()
                await asyncio.sleep(3)
                kept = band.client.is_connected
        except Exception as e:
            print(f"  Loi: {e}")
        finally:
            await band.close()
        print(f"  => {'GIU KET NOI' if kept else 'bi ngat'}")
        results.append((comp, kept))
        await asyncio.sleep(5)  # let the band drop the old session
    print("\nKET QUA:")
    for comp, kept in results:
        print(f"  {comp}: {'GIU KET NOI' if kept else 'bi ngat'}")


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--probe", action="store_true", help="chi in du lieu cam bien")
    p.add_argument("--debug", action="store_true", help="in moi lenh vong gui ve")
    p.add_argument("--diag", action="store_true", help="chan doan: vong co tu ngat ket noi khong")
    p.add_argument("--speed", type=float, default=600, help="do nhay (pixel moi rad/s)")
    p.add_argument("--deadzone", type=float, default=0.05, help="bo qua rung nho")
    p.add_argument("--calibrate", action="store_true", help="tu do huong xoay va luu cau hinh chuot")
    p.add_argument("--axis-x", default=None, choices="xyz", help="truc gyro dieu khien trai/phai")
    p.add_argument("--axis-y", default=None, choices="xyz", help="truc gyro dieu khien len/xuong")
    p.add_argument("--invert-x", action="store_true")
    p.add_argument("--invert-y", action="store_true")
    args = p.parse_args()

    with open(CONFIG) as f:
        cfg = json.load(f)
    open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "trace.log"), "w").close()

    if args.diag:
        await diagnose(cfg)
        return

    saved = cfg.get("mouse", {})
    if args.axis_x is None:
        args.axis_x = saved.get("axis_x", "z")
        args.invert_x = args.invert_x or saved.get("invert_x", False)
    if args.axis_y is None:
        args.axis_y = saved.get("axis_y", "y")
        args.invert_y = args.invert_y or saved.get("invert_y", False)

    if args.calibrate:
        handler = Calibrator()
    else:
        handler = Probe() if args.probe else Mouse(args)
    band = Band(cfg["ble_address"], cfg["auth_key"], on_sensor=handler, debug=args.probe or args.debug or args.diag)
    print("Dang ket noi vong...")
    try:
        await band.connect()
        print("Xac thuc thanh cong!")
        band.request_device_info()
        await asyncio.sleep(2)
        print("Dang bat luong cam bien (vong se hien man hinh tap luyen)...")
        await band.start_sensor_stream()
        if args.calibrate:
            await asyncio.sleep(2)  # let the stream start
            await handler.run(cfg)
        elif args.probe:
            await asyncio.sleep(math.inf)
        else:
            await handler.run()
    finally:
        print("\nDang dung...")
        await band.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        print("LOI:", e)
