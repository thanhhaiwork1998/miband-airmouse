"""Minimal Xiaomi Smart Band 10 client (BLE V2 protocol), ported from Gadgetbridge.

Only what the air mouse needs: session setup, encrypted auth with the auth key, and the
hidden "synthetic workout" that makes the band stream raw accelerometer + gyro batches.
"""
import asyncio
import hashlib
import hmac
import os
import struct
import sys
import time

from Crypto.Cipher import AES
from bleak import BleakClient, BleakScanner

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "xproto"))
import xiaomi_pb2  # noqa: E402
from xiaomi import wear_account_pb2, wear_fitness_pb2  # noqa: E402

RX_UUID = "0000005e-0000-1000-8000-00805f9b34fb"  # band -> us (notify)
TX_UUID = "0000005f-0000-1000-8000-00805f9b34fb"  # us -> band (write without response)

PREAMBLE = b"\xa5\xa5"
PKT_NACK, PKT_ACK, PKT_SESSION, PKT_DATA = 0, 1, 2, 3
CH_PROTOBUF = 1
OP_PLAIN, OP_ENCRYPTED = 1, 2

AUTH_TYPE, AUTH_NONCE, AUTH_CONFIRM = 1, 26, 27
SYSTEM_TYPE, S_DEVICE_INFO = 2, 2
HEALTH_TYPE = 8
H_WORKOUT_STATUS, H_WORKOUT_OPEN, H_STATS_PHONE, H_RAW_SENSOR = 26, 30, 49, 53
WORKOUT_STARTED, WORKOUT_FINISHED = 0, 3
SYNTHETIC_SPORT = 810  # hidden "motion sensing game" workout

# Session config payload copied from Gadgetbridge (packet dump of the official app)
SESSION_START = bytes([1, 1, 3, 0, 1, 0, 0, 2, 2, 0, 0, 0xfc, 3, 2, 0, 0x20, 0, 4, 2, 0, 0x10, 0x27])


def crc16_arc(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def frame(pkt_type, seq, payload):
    return PREAMBLE + struct.pack("<BBHH", pkt_type & 0xF, seq & 0xFF, len(payload), crc16_arc(payload)) + payload


def auth_keys(secret, phone_nonce, watch_nonce):
    key = hmac.new(phone_nonce + watch_nonce, secret, hashlib.sha256).digest()
    out, tmp, b = b"", b"", 1
    while len(out) < 64:
        tmp = hmac.new(key, tmp + b"miwear-auth" + bytes([b]), hashlib.sha256).digest()
        out += tmp
        b += 1
    return out[:16], out[16:32], out[32:36], out[36:40]  # dec key, enc key, dec nonce, enc nonce


class Band:
    def __init__(self, address, auth_key_hex, on_sensor=None, log=print, debug=False,
                 companion=(1, "iPhone15,2", 17)):  # band paired with an iPhone kicks Android identities
        self.address = address
        self.companion = companion  # (device type, phone name, api level) sent during auth
        self.debug = debug
        data = os.environ.get("AIRMOUSE_DATA", os.path.dirname(os.path.abspath(__file__)))
        self.trace = open(os.path.join(data, "trace.log"), "a") if debug else None
        self._trace(f"=== new session companion={companion}")
        self.secret = bytes.fromhex(auth_key_hex.lower().replace("0x", ""))[:16]
        self.on_sensor = on_sensor
        self.log = log
        self.client = None
        self.seq = 0
        self.rx = bytearray()
        self.tx_queue = asyncio.Queue()
        self.authed = asyncio.Event()
        self.disconnected = asyncio.Event()
        self._loop = asyncio.get_event_loop()
        self.auth_failed = None
        self.enc_key = self.dec_key = self.enc_nonce = None
        self.phone_nonce = os.urandom(16)
        self.connected_at = time.monotonic()
        self.workout_started_at = 0.0
        self.last_batch_at = 0.0
        self._tasks = []

    # ---------- transport ----------
    async def connect(self):
        dev = self._retrieve_from_macos()
        if dev:
            self.log("  macOS dang giu ket noi voi vong, dung luon ket noi do")
            self._trace(f"retrieved from macOS: {dev.address}")
        for attempt in range(1, 4):
            if dev:
                break
            self.log(f"  Tim vong (lan {attempt}/3)... cham vao man hinh vong cho sang")
            self._trace(f"scan attempt {attempt}")
            dev = await BleakScanner.find_device_by_address(self.address, timeout=15)
        if not dev:
            self._trace("band not found")
            raise RuntimeError("Khong thay vong. Mo sang man hinh vong, tat Bluetooth dien thoai roi thu lai.")
        self.log("  Da thay vong, dang ket noi...")
        self._trace("found, connecting")
        self.client = BleakClient(dev, timeout=60, disconnected_callback=self._on_disconnect)
        await self.client.connect()
        self._trace("connected")
        self.connected_at = time.monotonic()
        tx = self.client.services.get_characteristic(TX_UUID)
        self.chunk = max(20, getattr(tx, "max_write_without_response_size", 20))
        self._tasks.append(asyncio.ensure_future(self._writer(tx)))
        await self.client.start_notify(RX_UUID, self._on_notify)
        self._send_raw(frame(PKT_SESSION, 0, SESSION_START))
        try:
            await asyncio.wait_for(self.authed.wait(), 20)
        except asyncio.TimeoutError:
            raise RuntimeError(self.auth_failed or "Vong khong tra loi xac thuc (het thoi gian).")
        if self.auth_failed:
            raise RuntimeError(self.auth_failed)

    async def close(self):
        try:
            if self.workout_started_at:
                self._workout_status(WORKOUT_FINISHED)
                await asyncio.sleep(0.5)
        finally:
            for t in self._tasks:
                t.cancel()
            if self.client and self.client.is_connected:
                await self.client.disconnect()

    def _retrieve_from_macos(self):
        """Once macOS has bonded with the band it keeps it connected, and a connected band stops
        advertising, so scanning never finds it. Ask CoreBluetooth for the peripheral directly."""
        try:
            from CoreBluetooth import CBUUID
            from Foundation import NSUUID
            from bleak.backends.corebluetooth.CentralManagerDelegate import CentralManagerDelegate
            from bleak.backends.device import BLEDevice

            self._manager = CentralManagerDelegate.alloc().init()
            cm = self._manager.central_manager
            found = list(cm.retrieveConnectedPeripheralsWithServices_([CBUUID.UUIDWithString_("FE95")]))
            found += list(cm.retrievePeripheralsWithIdentifiers_([NSUUID.alloc().initWithUUIDString_(self.address)]))
            self._trace(f"macOS peripherals: {[(p.identifier().UUIDString(), p.name()) for p in found]}")
            for p in found:
                if p.identifier().UUIDString() == self.address or "Band" in (p.name() or ""):
                    return BLEDevice(p.identifier().UUIDString(), p.name(), (p, self._manager))
        except Exception as e:
            self._trace(f"retrieve failed: {type(e).__name__}: {e}")
        return None

    def _on_disconnect(self, _client):
        self._trace("DISCONNECTED")
        self._loop.call_soon_threadsafe(self.disconnected.set)
        self.log(f"  !! Vong da ngat ket noi (t={time.monotonic() - self.connected_at:.1f}s sau khi ket noi)")

    def _send_raw(self, data):
        self.tx_queue.put_nowait(data)

    async def _writer(self, tx):
        self._trace(f"write chunk size={self.chunk}")
        while True:
            data = await self.tx_queue.get()
            try:
                for i in range(0, len(data), self.chunk):
                    await self.client.write_gatt_char(tx, data[i:i + self.chunk], response=False)
                self._trace(f"TX raw {data[:24].hex()} ({len(data)} byte)")
            except Exception as e:
                self._trace(f"TX FAIL {type(e).__name__}: {e}")
                self.log(f"  LOI gui du lieu: {type(e).__name__}: {e}")

    def _send_command(self, cmd):
        body = cmd.SerializeToString()
        if cmd.type == AUTH_TYPE:
            payload = bytes([CH_PROTOBUF, OP_PLAIN]) + body
        else:
            payload = bytes([CH_PROTOBUF, OP_ENCRYPTED]) + self._ctr(self.enc_key, body)
        self._trace(f"TX data seq={self.seq} cmd type={cmd.type} subtype={cmd.subtype} plain={body[:48].hex()}")
        self._send_raw(frame(PKT_DATA, self.seq, payload))
        self.seq = (self.seq + 1) & 0xFF

    @staticmethod
    def _ctr(key, data):
        # Yes, the band uses the key as the IV too
        return AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=key).encrypt(data)

    def _on_notify(self, _char, value):
        self.rx += value
        while True:
            start = self.rx.find(PREAMBLE)
            if start < 0:
                self.rx.clear()
                return
            del self.rx[:start]
            if len(self.rx) < 8:
                return
            pkt_type = self.rx[2] & 0xF
            seq = self.rx[3]
            length, checksum = struct.unpack_from("<HH", self.rx, 4)
            if len(self.rx) < 8 + length:
                return
            payload = bytes(self.rx[8:8 + length])
            del self.rx[:8 + length]
            if crc16_arc(payload) != checksum:
                self.log("  (bo qua goi tin sai checksum)")
                continue
            self._on_packet(pkt_type, seq, payload)

    def _trace(self, msg):
        if self.trace:
            self.trace.write(f"{time.monotonic():.3f} {msg}\n")
            self.trace.flush()

    def _on_packet(self, pkt_type, seq, payload):
        self._trace(f"RX type={pkt_type} seq={seq} len={len(payload)} {payload[:48].hex()}")
        if self.debug and pkt_type == PKT_NACK:
            self.log(f"  <- NACK: vong cho goi so {seq}, minh dang o so {self.seq}")
        if self.debug and pkt_type == PKT_DATA and payload and payload[0] & 0xF != CH_PROTOBUF:
            self.log(f"  <- du lieu kenh {payload[0] & 0xF}, {len(payload)} byte")
        if pkt_type == PKT_SESSION:
            self._send_command(self._nonce_command())
        elif pkt_type == PKT_DATA and len(payload) >= 2:
            self._send_raw(frame(PKT_ACK, seq, b""))
            channel, opcode, body = payload[0] & 0xF, payload[1], payload[2:]
            if opcode == OP_ENCRYPTED:
                body = self._ctr(self.dec_key, body)
            self._trace(f"   data ch={channel} op={opcode} body={body[:64].hex()}")
            if channel == CH_PROTOBUF:
                cmd = xiaomi_pb2.Command()
                try:
                    cmd.ParseFromString(body)
                except Exception as e:
                    self._trace(f"   PARSE FAIL {e}")
                    if self.debug:
                        self.log(f"  (khong doc duoc goi protobuf {len(body)} byte)")
                    return
                self._trace(f"   cmd type={cmd.type} subtype={cmd.subtype}")
                self._on_command(cmd)

    # ---------- auth ----------
    def _nonce_command(self):
        cmd = xiaomi_pb2.Command(type=AUTH_TYPE, subtype=AUTH_NONCE)
        cmd.auth.appVerify.nonce = self.phone_nonce
        return cmd

    def request_device_info(self):
        self._send_command(xiaomi_pb2.Command(type=SYSTEM_TYPE, subtype=S_DEVICE_INFO))

    def _on_command(self, cmd):
        if self.debug and not (cmd.type == HEALTH_TYPE and cmd.subtype == H_RAW_SENSOR):
            fields = ",".join(f.name for f, _ in cmd.ListFields() if f.name not in ("type", "subtype"))
            self.log(f"  <- lenh type={cmd.type} subtype={cmd.subtype} [{fields}]")
            if cmd.type == HEALTH_TYPE and cmd.HasField("health"):
                self.log(f"     {str(cmd.health).strip().replace(chr(10), ' | ')[:300]}")
        if cmd.type == SYSTEM_TYPE and cmd.subtype == S_DEVICE_INFO:
            info = cmd.system.deviceInfo
            self.log(f"  Thong tin vong: model={info.model} firmware={info.firmware}")
        if cmd.type == AUTH_TYPE:
            self._on_auth(cmd)
        elif cmd.type == HEALTH_TYPE:
            self._on_health(cmd)

    def _on_auth(self, cmd):
        if cmd.subtype == AUTH_NONCE:
            dv = cmd.auth.deviceVerify
            self.dec_key, self.enc_key, _dec_nonce, self.enc_nonce = auth_keys(self.secret, self.phone_nonce, dv.nonce)
            expected = hmac.new(self.dec_key, dv.nonce + self.phone_nonce, hashlib.sha256).digest()
            if expected != dv.hmac:
                self.auth_failed = "Sai auth key (vong tu choi). Lay lai key bang token_extractor."
                self.authed.set()
                return
            phone_type, phone_name, api_level = self.companion
            info = wear_account_pb2.CompanionDevice(unknown1=phone_type, phoneApiLevel=api_level,
                                                    phoneName=phone_name, unknown3=224, region="VN")
            ccm_nonce = self.enc_nonce + struct.pack("<II", 0, 0)
            cipher = AES.new(self.enc_key, AES.MODE_CCM, nonce=ccm_nonce, mac_len=4)
            enc_info = cipher.encrypt(info.SerializeToString()) + cipher.digest()
            reply = xiaomi_pb2.Command(type=AUTH_TYPE, subtype=AUTH_CONFIRM)
            reply.auth.appConfirm.encryptedNonces = hmac.new(
                self.enc_key, self.phone_nonce + dv.nonce, hashlib.sha256).digest()
            reply.auth.appConfirm.encryptedDeviceInfo = enc_info
            self._send_command(reply)
        elif cmd.subtype == AUTH_CONFIRM:
            self.authed.set()
        else:
            # Gadgetbridge ignores other auth subtypes too (e.g. 16, sent unprompted)
            self.log(f"  (bo qua lenh auth subtype={cmd.subtype})")

    # ---------- raw sensor stream ----------
    async def start_sensor_stream(self):
        self._workout_status(WORKOUT_FINISHED)  # close a workout the band may still hold
        await asyncio.sleep(0.5)
        self.workout_started_at = self.last_batch_at = time.monotonic()
        self._workout_status(WORKOUT_STARTED)
        self._tasks.append(asyncio.ensure_future(self._stats_ticker()))
        self._tasks.append(asyncio.ensure_future(self._keepalive()))

    def _workout_status(self, status):
        cmd = xiaomi_pb2.Command(type=HEALTH_TYPE, subtype=H_WORKOUT_STATUS)
        ws = cmd.health.workoutStatusWatch
        ws.timestamp = int(time.time())
        ws.sportInfo.tzOffsetQuarterHours = int(time.localtime().tm_gmtoff // 900)
        ws.sport = SYNTHETIC_SPORT
        ws.status = status
        ws.supportedVersions = 3
        self._send_command(cmd)

    async def _stats_ticker(self):
        # The band blanks its workout screen if these stop
        while True:
            cmd = xiaomi_pb2.Command(type=HEALTH_TYPE, subtype=H_STATS_PHONE)
            st = cmd.health.workoutStatsPhone
            st.durationSeconds = int(time.monotonic() - self.workout_started_at)
            st.heartRate, st.calories, st.distance = 255, 0, 0
            self._send_command(cmd)
            await asyncio.sleep(1 if time.monotonic() - self.workout_started_at < 10 else 5)

    async def _keepalive(self):
        while True:
            await asyncio.sleep(24)
            if time.monotonic() - self.last_batch_at > 30:
                self.log("  Khong nhan du lieu 30s, khoi dong lai luong cam bien...")
                self._workout_status(WORKOUT_FINISHED)
                await asyncio.sleep(0.5)
                self.last_batch_at = time.monotonic()
            self._workout_status(WORKOUT_STARTED)

    def _on_health(self, cmd):
        if cmd.subtype == H_WORKOUT_OPEN:
            reply = xiaomi_pb2.Command(type=HEALTH_TYPE, subtype=H_WORKOUT_OPEN)
            r = reply.health.workoutOpenReply
            r.code, r.selectedVersion, r.gpsAccuracy = 0, 2, 2
            self._send_command(reply)
        elif cmd.subtype == H_RAW_SENSOR:
            self.last_batch_at = time.monotonic()
            if self.on_sensor:
                self.on_sensor(cmd.health.rawSensorBatch)
