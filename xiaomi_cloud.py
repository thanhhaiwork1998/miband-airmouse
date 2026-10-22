"""Getting a band's token from the Xiaomi cloud by QR-code login, inside the app.

The login and the encrypted API calls are ported from Xiaomi-cloud-tokens-extractor by
Piotr Machowski (MIT License, https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor);
only the QR flow and the device listing are kept. No password ever passes through this app:
the user approves the login on their phone (Mi Fitness / Xiaomi Home).
"""
import base64
import hashlib
import json
import os
import random
import time

import requests
from Crypto.Cipher import ARC4

SERVERS = ["sg", "de", "us", "cn", "ru", "tw", "in", "i2"]  # Vietnam accounts are usually on "sg"


class QrLogin:
    def __init__(self):
        self._session = requests.session()
        self._agent = (''.join(chr(random.randint(97, 122)) for _ in range(18)) + "-" +
                       ''.join(chr(random.randint(65, 69)) for _ in range(13)) +
                       " APP/com.xiaomi.mihome APPV/10.5.201")
        self.user_id = self._ssecurity = self._service_token = None
        self._lp_url = self._timeout = None
        self.login_url = None

    # ---------- login ----------
    def start(self):
        """Ask for a login QR code. Returns the QR image bytes (PNG/JPEG)."""
        r = self._session.get("https://account.xiaomi.com/longPolling/loginUrl", params={
            "_qrsize": "480", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "callback": "https://sts.api.io.mi.com/sts", "_hasLogo": "false", "sid": "xiaomiio",
            "serviceParam": "", "_locale": "en_GB", "_dc": str(int(time.time() * 1000))}, timeout=15)
        data = _json(r.text)
        if r.status_code != 200 or "qr" not in data:
            raise RuntimeError("Xiaomi không trả về mã QR")
        self._lp_url, self._timeout, self.login_url = data["lp"], data.get("timeout", 300), data["loginUrl"]
        img = self._session.get(data["qr"], timeout=15)
        if img.status_code != 200:
            raise RuntimeError("Không tải được ảnh mã QR")
        return img.content

    def wait(self, cancelled):
        """Long-poll until the QR is scanned and approved (True), it expires or `cancelled()`."""
        start = time.time()
        while not cancelled():
            try:
                r = self._session.get(self._lp_url, timeout=10)
            except requests.exceptions.Timeout:
                if time.time() - start > self._timeout:
                    return False
                continue
            if r.status_code == 200:
                data = _json(r.text)
                self.user_id, self._ssecurity = data["userId"], data["ssecurity"]
                st = self._session.get(data["location"], headers={"content-type": "application/x-www-form-urlencoded"},
                                       timeout=15)
                self._service_token = st.cookies.get("serviceToken")
                return bool(self._service_token)
            if time.time() - start > self._timeout:
                return False
        return False

    # ---------- devices ----------
    def wearables(self):
        """Xiaomi wearables (bands/watches) on the account, from every server:
        [{"name", "model", "mac", "token"}]."""
        found = {}
        for server in SERVERS:
            homes = []
            res = self._call(server, "/v2/homeroom/gethome",
                             '{"fg": true, "fetch_share": true, "fetch_share_dev": true, "limit": 300, "app_ver": 7}')
            for h in ((res or {}).get("result") or {}).get("homelist") or []:
                homes.append((h["id"], self.user_id))
            res = self._call(server, "/v2/user/get_device_cnt", '{ "fetch_own": true, "fetch_share": true}')
            for h in (((res or {}).get("result") or {}).get("share") or {}).get("share_family") or []:
                homes.append((h["home_id"], h["home_owner"]))
            for home_id, owner in homes:
                res = self._call(server, "/v2/home/home_device_list",
                                 '{"home_owner": %s,"home_id": %s,  "limit": 200,  "get_split_device": true, '
                                 '"support_smart_home": true}' % (owner, home_id))
                for d in ((res or {}).get("result") or {}).get("device_info") or []:
                    model = d.get("model", "")
                    if d.get("token") and ("miwear" in model or "band" in d.get("name", "").lower()):
                        found[d.get("mac") or d["token"]] = {"name": d.get("name", model), "model": model,
                                                             "mac": (d.get("mac") or "").upper(), "token": d["token"]}
        return list(found.values())

    def _call(self, server, path, data):
        url = "https://" + ("" if server == "cn" else server + ".") + "api.io.mi.com/app" + path
        nonce = base64.b64encode(os.urandom(8) + int(time.time() * 1000 / 60000).to_bytes(4, "big")).decode()
        signed = _signed_nonce(self._ssecurity, nonce)
        params = {"data": data}
        params["rc4_hash__"] = _enc_signature(url, signed, params)
        params = {k: _rc4(signed, v) for k, v in params.items()}
        params.update(signature=_enc_signature(url, signed, params), ssecurity=self._ssecurity, _nonce=nonce)
        try:
            r = self._session.post(url, params=params, timeout=15, headers={
                "Accept-Encoding": "identity", "User-Agent": self._agent,
                "Content-Type": "application/x-www-form-urlencoded",
                "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2", "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4"},
                cookies={"userId": str(self.user_id), "yetAnotherServiceToken": str(self._service_token),
                         "serviceToken": str(self._service_token), "locale": "en_GB", "timezone": "GMT+07:00",
                         "is_daylight": "0", "dst_offset": "0", "channel": "MI_APP_STORE"})
        except requests.exceptions.RequestException:
            return None
        if r.status_code != 200:
            return None
        try:
            return json.loads(_rc4_decrypt(_signed_nonce(self._ssecurity, nonce), r.text))
        except (ValueError, UnicodeDecodeError):
            return None


def _json(text):
    try:
        return json.loads(text.replace("&&&START&&&", ""))
    except ValueError:
        return {}


def _signed_nonce(ssecurity, nonce):
    return base64.b64encode(hashlib.sha256(base64.b64decode(ssecurity) + base64.b64decode(nonce)).digest()).decode()


def _enc_signature(url, signed_nonce, params):
    parts = ["POST", url.split("com")[1].replace("/app/", "/")] + [f"{k}={v}" for k, v in params.items()] + [signed_nonce]
    return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()


def _rc4(key, text):
    c = ARC4.new(base64.b64decode(key))
    c.encrypt(bytes(1024))
    return base64.b64encode(c.encrypt(text.encode())).decode()


def _rc4_decrypt(key, b64):
    c = ARC4.new(base64.b64decode(key))
    c.encrypt(bytes(1024))
    return c.encrypt(base64.b64decode(b64))
