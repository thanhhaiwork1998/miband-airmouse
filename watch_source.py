"""Apple Watch as the sensor source: the Mac acts as a Bluetooth peripheral and the watch app
(~/airmouse/watch/) connects to it and streams CoreMotion samples.

Packet (watch -> Mac, write without response), little endian:
    u8  0xA1            magic / version
    u8  n               samples in this packet (<= 10)
    u64 t0              timestamp of the first sample, microseconds (watch clock)
    n x {
        u16 dt          offset from t0 in units of 10 us
        i16 gx, gy, gz  rotation rate, mrad/s (watch frame, bias-corrected by CoreMotion)
        i16 fx, fy, fz  specific force, milli-g: -(gravity + userAcceleration), i.e. +1 g
                        upward at rest - the same convention as the Mi Band's accelerometer
    }
Samples are handed to the pointer in the Mi Band's batch shape (gyro rad/s, accel m/s^2,
timestamps in us), so every gesture works unchanged.
"""
import struct
import threading
import time
import types

import objc
from CoreBluetooth import (CBAdvertisementDataLocalNameKey, CBAdvertisementDataServiceUUIDsKey,
                           CBAttributePermissionsWriteable, CBCharacteristicPropertyWrite,
                           CBCharacteristicPropertyWriteWithoutResponse, CBMutableCharacteristic,
                           CBMutableService, CBPeripheralManager, CBUUID)
from Foundation import NSObject
from libdispatch import DISPATCH_QUEUE_SERIAL, dispatch_queue_create

SERVICE_UUID = "6E7A0001-5C3A-4F1B-9C2D-A1B2C3D4E5F6"
DATA_UUID = "6E7A0002-5C3A-4F1B-9C2D-A1B2C3D4E5F6"
LOCAL_NAME = "AirMouse Mac"
MAGIC = 0xA1
HEADER = struct.Struct("<BBQ")
SAMPLE = struct.Struct("<H6h")
G = 9.81
POWERED_ON = 5  # CBManagerStatePoweredOn


def parse_packet(data):
    """Bytes from the watch -> a batch shaped like the Mi Band's (or None if malformed)."""
    if len(data) < HEADER.size:
        return None
    magic, n, t0 = HEADER.unpack_from(data)
    if magic != MAGIC or len(data) < HEADER.size + n * SAMPLE.size:
        return None
    gyro, accel = [], []
    for i in range(n):
        dt, gx, gy, gz, fx, fy, fz = SAMPLE.unpack_from(data, HEADER.size + i * SAMPLE.size)
        ts = t0 + dt * 10
        gyro.append(types.SimpleNamespace(x=gx / 1000, y=gy / 1000, z=gz / 1000, timestamp=ts))
        accel.append(types.SimpleNamespace(x=fx * G / 1000, y=fy * G / 1000, z=fz * G / 1000, timestamp=ts))
    return types.SimpleNamespace(gyro=gyro, accel=accel)


class _PeripheralDelegate(NSObject):
    def initWithSource_(self, source):
        self = objc.super(_PeripheralDelegate, self).init()
        if self is None:
            return None
        self.source = source
        return self

    def peripheralManagerDidUpdateState_(self, manager):
        self.source._state_changed(manager.state())

    def peripheralManager_didAddService_error_(self, manager, service, error):
        if error is not None:
            self.source.log(f"watch: add service failed: {error}")
            return
        manager.startAdvertising_({CBAdvertisementDataLocalNameKey: LOCAL_NAME,
                                   CBAdvertisementDataServiceUUIDsKey: [CBUUID.UUIDWithString_(SERVICE_UUID)]})

    def peripheralManagerDidStartAdvertising_error_(self, manager, error):
        self.source.log(f"watch: advertising {'failed: ' + str(error) if error else 'as ' + LOCAL_NAME}")

    def peripheralManager_didReceiveWriteRequests_(self, manager, requests):
        for req in requests:
            value = req.value()
            if value is not None:
                self.source._received(bytes(value))
        # Writes with response (not used by the watch app) still need an answer
        if requests and requests[0].characteristic().properties() & CBCharacteristicPropertyWrite:
            manager.respondToRequest_withResult_(requests[0], 0)


class WatchSource:
    """Advertises the AirMouse service and feeds the watch's samples to `on_batch`, which is
    called on the asyncio loop `loop` (the engine's), like the Mi Band's batches."""

    def __init__(self, loop, on_batch, log):
        self.loop, self.on_batch, self.log = loop, on_batch, log
        self.last_packet = 0.0
        self.packets = 0
        self.bad = 0
        self._queue = dispatch_queue_create(b"airmouse.watch", DISPATCH_QUEUE_SERIAL)
        self._delegate = _PeripheralDelegate.alloc().initWithSource_(self)
        self._manager = None
        self._lock = threading.Lock()

    def start(self):
        self._manager = CBPeripheralManager.alloc().initWithDelegate_queue_options_(
            self._delegate, self._queue, None)

    def stop(self):
        if self._manager is not None:
            self._manager.stopAdvertising()
            self._manager.removeAllServices()
            self._manager = None

    def active(self, within=2.0):
        """The watch is streaming (a packet arrived recently)."""
        return time.monotonic() - self.last_packet < within

    # ---- called on the CoreBluetooth queue ----
    def _state_changed(self, state):
        if state != POWERED_ON:
            self.log(f"watch: Bluetooth state {state}")
            return
        char = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            CBUUID.UUIDWithString_(DATA_UUID),
            CBCharacteristicPropertyWriteWithoutResponse | CBCharacteristicPropertyWrite,
            None, CBAttributePermissionsWriteable)
        service = CBMutableService.alloc().initWithType_primary_(CBUUID.UUIDWithString_(SERVICE_UUID), True)
        service.setCharacteristics_([char])
        self._manager.addService_(service)

    def _received(self, data):
        batch = parse_packet(data)
        if batch is None:
            self.bad += 1
            return
        if not self.active():
            self.log("watch: streaming started")
        self.last_packet = time.monotonic()
        self.packets += 1
        self.loop.call_soon_threadsafe(self.on_batch, batch)
