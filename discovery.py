"""Finding Xiaomi bands nearby, for the setup window."""
import asyncio

from bleak import BleakScanner

FE95 = "0000fe95-0000-1000-8000-00805f9b34fb"  # Xiaomi's BLE service (MiBeacon advertising)


def mac_from_mibeacon(data):
    """MiBeacon service data: frame control (2) + product id (2) + counter (1) + MAC reversed (6)
    when frame-control bit 0x10 says the MAC is included."""
    if len(data) < 11 or not int.from_bytes(data[:2], "little") & 0x10:
        return None
    return ":".join(f"{b:02X}" for b in reversed(data[5:11]))


def connected_bands():
    """Bands macOS already holds a connection to (bonded bands stop advertising, so a scan
    can't see them)."""
    try:
        from CoreBluetooth import CBUUID
        from bleak.backends.corebluetooth.CentralManagerDelegate import CentralManagerDelegate
        manager = CentralManagerDelegate.alloc().init()
        found = manager.central_manager.retrieveConnectedPeripheralsWithServices_([CBUUID.UUIDWithString_("FE95")])
        return [{"address": p.identifier().UUIDString(), "name": p.name() or "Thiết bị Xiaomi",
                 "mac": None, "rssi": None, "connected": True} for p in found]
    except Exception:
        return []


async def find_bands(timeout=8.0):
    """Xiaomi bands nearby: connected ones first, then advertising ones by signal strength."""
    bands = {b["address"]: b for b in connected_bands()}
    seen = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for address, (device, adv) in seen.items():
        name = adv.local_name or device.name or ""
        data = adv.service_data.get(FE95)
        if not data and not any(k in name for k in ("Band", "Xiaomi")):
            continue
        entry = bands.setdefault(address, {"address": address, "connected": False})
        entry.update(name=name or entry.get("name") or "Thiết bị Xiaomi (không tên)",
                     mac=mac_from_mibeacon(data) if data else entry.get("mac"), rssi=adv.rssi)
    return sorted(bands.values(), key=lambda b: (not b["connected"], -(b["rssi"] or -999)))


def describe(band):
    parts = [band["name"]]
    if band.get("mac"):
        parts.append(band["mac"])
    parts.append("đang kết nối với Mac" if band["connected"] else f"tín hiệu {band['rssi']} dBm")
    return " · ".join(parts)


if __name__ == "__main__":
    for b in asyncio.run(find_bands()):
        print(describe(b), b["address"])
