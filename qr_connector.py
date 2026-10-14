import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import struct
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import parse_qsl, urlencode, urlsplit

import aiohttp
from Crypto.Cipher import ARC4

START_CONSENT = "community-qr-c4db715-v1"
BIND_CONSENT = "save-fixed-device-and-protected-control-v1"
TARGET = {"ip": "192.168.3.24", "model": "zhimi.airpurifier.m2",
          "did": "70632677", "mac": "78:11:dc:47:87:bf"}
logger = logging.getLogger(__name__)


class QRError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def strict_json(raw):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise QRError("invalid_input")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=object_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise QRError("invalid_input") from None


def checked_url(url, stage):
    rules = {
        "create": ("account.xiaomi.com", "/longPolling/loginUrl",
                   {"_qrsize", "qs", "callback", "_hasLogo", "sid", "serviceParam", "_locale", "_dc"}),
        "image": ("account.xiaomi.com", "/pass/qr/login", {"ticket", "dc", "sid", "_qrsize", "_hasLogo", "ts"}),
        "poll": ("c3.lp.account.xiaomi.com", "/lp/s", {"k"}),
        "sts": ("sts.api.io.mi.com", "/sts", {"d", "ticket", "sid", "nonce", "clientSign", "p", "sign", "_ssign", "_slh",
                                            "pwd", "p_ts", "fid", "p_lm", "auth", "m", "_group", "tsl", "p_ca", "p_ur", "p_idc"}),
    }
    if not isinstance(url, str) or len(url) > 4096 or any(ord(char) < 33 for char in url):
        logger.warning("[qr-diagnostic] stage=checked_url reason=url_shape")
        raise QRError("unsupported_protocol")
    reason = "authority"
    try:
        parts = urlsplit(url)
        if stage == "api":
            host, path, keys = "api.io.mi.com", parts.path, set()
            if path not in ("/app/v2/homeroom/gethome", "/app/v2/user/get_device_cnt", "/app/v2/home/home_device_list",
                            "/app/home/device_list"):
                reason = "path"
                raise ValueError
        else:
            reason = "stage"
            host, path, keys = rules[stage]
            if stage == "poll" and parts.hostname == "ak.lp.account.xiaomi.com":
                host = parts.hostname
        reason = "query_syntax"
        max_fields = len(keys) if stage == "sts" else 12
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=max_fields)
        if (parts.scheme != "https" or parts.hostname != host or parts.netloc not in (host, host + ":443")
                or parts.path != path or parts.fragment or parts.username or parts.password
                or len({key for key, value in pairs}) != len(pairs)
                or any(key not in keys or len(value) > 2048 for key, value in pairs)):
            if parts.scheme != "https":
                reason = "scheme"
            elif parts.username or parts.password:
                reason = "credentials"
            elif parts.hostname != host or parts.netloc not in (host, host + ":443"):
                reason = "authority"
            elif parts.path != path:
                reason = "path"
            elif parts.fragment:
                reason = "fragment"
            elif len({key for key, value in pairs}) != len(pairs):
                reason = "query_duplicate"
            elif any(key not in keys for key, value in pairs):
                reason = "query_unknown"
            else:
                reason = "query_value_length"
            raise ValueError
        values = dict(pairs)
        if "sid" in values and values["sid"] != "xiaomiio":
            reason = "sid"
            raise ValueError
        if stage == "poll" and (set(values) != {"k"} or not values["k"]):
            reason = "poll_key"
            raise ValueError
        if stage == "image" and (not values.get("ticket") or values.get("_qrsize") != "480"):
            reason = "image_ticket_size"
            raise ValueError
    except (ValueError, KeyError):
        if reason == "query_syntax":
            reason = ("query_field_limit" if parts.query.count("&") >= max_fields else
                      "query_empty_field" if "" in parts.query.split("&") else "query_bare_field")
        logger.warning("[qr-diagnostic] stage=checked_url reason=%s", reason)
        raise QRError("unsupported_protocol") from None
    return url


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM, family=family)
        if not records or any(not ipaddress.ip_address(record[4][0]).is_global for record in records):
            raise QRError("unsafe_network")
        return [{"hostname": host, "host": record[4][0], "port": port, "family": record[0],
                 "proto": record[2], "flags": socket.AI_NUMERICHOST} for record in records]

    async def close(self):
        pass


def rc4(key, data):
    cipher = ARC4.new(base64.b64decode(key, validate=True))
    cipher.encrypt(bytes(1024))
    return cipher.encrypt(data)


def signature(path, key, params):
    text = "&".join(["POST", path.removeprefix("/app"),
                     *[f"{name}={value}" for name, value in params.items()], key])
    return base64.b64encode(hashlib.sha1(text.encode()).digest()).decode()


def encrypted_fields(path, security, nonce, data):
    key = base64.b64encode(hashlib.sha256(base64.b64decode(security, validate=True)
                                        + base64.b64decode(nonce, validate=True)).digest()).decode()
    params = {"data": json.dumps(data, separators=(",", ":"))}
    params["rc4_hash__"] = signature(path, key, params)
    params = {name: base64.b64encode(rc4(key, value.encode())).decode() for name, value in params.items()}
    params.update(signature=signature(path, key, params), ssecurity=security, _nonce=nonce)
    return key, params


def positive_id(value):
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", text):
        raise QRError("unsupported_protocol")
    return int(text)


class Cloud:
    def __init__(self):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context, resolver=PublicResolver(),
                                               use_dns_cache=False, limit=2), trust_env=False,
                       cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
                       headers={"Accept-Encoding": "identity", "User-Agent": os.urandom(9).hex()
                                + "-ABCDEABCDEABC APP/com.xiaomi.mihome APPV/10.5.201"})
        self.requests, self.bytes = 0, 0

    async def request(self, url, stage, *, data=None, cookies=None):
        checked_url(url, stage)
        self.requests += 1
        if self.requests > 64:
            raise QRError("response_limit")
        async with self.session.request("POST" if data is not None else "GET", url,
                data=data, cookies=cookies, allow_redirects=False,
                headers={"MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4", "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2"}
                        if stage == "api" else {}, timeout=aiohttp.ClientTimeout(total=12, connect=5)) as response:
            diagnostic_stage = next((name for name in ("create", "image", "poll", "sts", "api")
                                     if name == stage), "transport")
            if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                logger.warning("[qr-diagnostic] stage=%s reason=encoding", diagnostic_stage)
                raise QRError("unsupported_protocol")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                raw.extend(chunk)
                self.bytes += len(chunk)
                if len(raw) > (262144 if stage == "image" else 2097152) or self.bytes > 8388608:
                    raise QRError("response_limit")
            if response.status != 200:
                logger.warning("[qr-diagnostic] stage=%s reason=http_status status=%d", diagnostic_stage, response.status)
                raise QRError("cloud_request_failed")
            token = response.cookies.get("serviceToken")
            return bytes(raw), token.value if stage == "sts" and token is not None else None

    async def api(self, path, data, uid, security, service_token):
        nonce = base64.b64encode(os.urandom(8) + (int(time.time() / 60)).to_bytes(4, "big")).decode()
        key, fields = encrypted_fields(path, security, nonce, data)
        raw, _ = await self.request("https://api.io.mi.com" + path, "api", data=fields,
                  cookies={"userId": str(uid), "serviceToken": service_token, "yetAnotherServiceToken": service_token,
                           "locale": "en_GB", "timezone": "GMT+08:00", "channel": "MI_APP_STORE"})
        value = strict_json(rc4(key, base64.b64decode(raw, validate=True)))
        if not isinstance(value, dict) or value.get("code") != 0 or not isinstance(value.get("result"), dict):
            raise QRError("cloud_request_failed")
        return value["result"]

    async def close(self):
        await asyncio.wait_for(self.session.close(), timeout=5)


class Phase(str, Enum):
    PREPARING = "preparing"
    WAITING = "waiting"
    FETCHING = "fetching"
    READY = "ready"
    QUEUED = "bind_queued"
    BINDING = "binding"
    BOUND = "bound"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"


@dataclass
class Session:
    owner: str = field(repr=False)
    origin: str
    deadline: float
    phase: Phase = Phase.PREPARING
    code: str = ""
    image: bytes = field(default=b"", repr=False)
    token: str = field(default="", repr=False)
    devices: dict = field(default_factory=dict, repr=False)
    acquisition: object = field(default=None, repr=False)
    expiry: object = field(default=None, repr=False)
    binding: object = field(default=None, repr=False)


class QRConnector:
    def __init__(self, pair, cloud_factory=Cloud):
        self.pair, self.cloud_factory = pair, cloud_factory
        self.session, self.closing = None, False

    def owned(self, owner, origin):
        session = self.session
        if session is None:
            raise QRError("session_missing", 410)
        if not hmac.compare_digest(session.owner, owner) or session.origin != origin:
            raise QRError("session_busy", 409)
        self.check_expiry(session)
        return session

    def check_expiry(self, session):
        if time.monotonic() >= session.deadline and session.phase in (
                Phase.PREPARING, Phase.WAITING, Phase.FETCHING, Phase.READY, Phase.QUEUED):
            session.phase, session.code = Phase.EXPIRED, "session_expired"
            self.clear(session)

    def clear(self, session):
        session.token, session.image = "", b""
        session.devices.clear()
        if (session.acquisition and not session.acquisition.done() and not session.acquisition.cancelling()
                and session.acquisition is not asyncio.current_task()):
            session.acquisition.cancel()
        if (session.phase not in (Phase.PREPARING, Phase.WAITING, Phase.FETCHING, Phase.READY, Phase.QUEUED)
                and session.expiry and session.expiry is not asyncio.current_task()):
            session.expiry.cancel()

    def public(self, session):
        self.check_expiry(session)
        return {"phase": session.phase.value, "remaining_seconds": max(0, int(session.deadline - time.monotonic())),
                "code": session.code, "target": dict(TARGET), "image_ready": bool(session.image),
                "devices": [{key: device[key] for key in ("did", "name", "model", "ip", "online", "selectable", "reason")}
                            for device in session.devices.values()]}

    def start(self, owner, origin):
        if self.closing:
            raise QRError("service_closing", 503)
        previous = self.session
        if previous:
            self.check_expiry(previous)
            if previous.phase not in (Phase.CANCELED, Phase.EXPIRED, Phase.FAILED, Phase.BOUND):
                return self.public(self.owned(owner, origin))
            if hmac.compare_digest(previous.owner, owner) and previous.origin == origin:
                return self.public(previous)
            if any(task and not task.done() for task in (previous.acquisition, previous.binding, previous.expiry)):
                raise QRError("cleanup_pending", 409)
        session = Session(owner, origin, time.monotonic() + 180)
        self.session = session
        session.acquisition = asyncio.create_task(self.acquire(session))
        session.expiry = asyncio.create_task(self.expire(session))
        return self.public(session)

    async def expire(self, session):
        await asyncio.sleep(max(0, session.deadline - time.monotonic()))
        self.check_expiry(session)

    async def acquire(self, session):
        cloud = None
        ready_token, ready_devices = None, {}
        stage = "create"
        try:
            async with asyncio.timeout(180):
                cloud = self.cloud_factory()
                params = {"_qrsize": "480", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
                          "callback": "https://sts.api.io.mi.com/sts", "_hasLogo": "false", "sid": "xiaomiio",
                          "serviceParam": "", "_locale": "en_GB", "_dc": str(int(time.time() * 1000))}
                raw, _ = await cloud.request("https://account.xiaomi.com/longPolling/loginUrl?" + urlencode(params), "create")
                metadata = strict_json(raw.removeprefix(b"&&&START&&&"))
                del raw
                timeout = metadata["timeout"]
                if type(timeout) not in (int, float) or not 0 < timeout <= 600:
                    raise QRError("unsupported_protocol")
                session.deadline = min(session.deadline, time.monotonic() + timeout)
                session.expiry.cancel()
                session.expiry = asyncio.create_task(self.expire(session))
                stage = "image"
                image, _ = await cloud.request(checked_url(metadata["qr"], "image"), "image")
                if (len(image) < 33 or image[:8] != b"\x89PNG\r\n\x1a\n" or image[12:16] != b"IHDR"
                        or struct.unpack("!II", image[16:24]) != (480, 480)):
                    raise QRError("unsupported_protocol")
                session.image, session.phase = image, Phase.WAITING
                del image
                stage = "poll"
                poll_url = checked_url(metadata["lp"], "poll")
                for _ in range(24):
                    self.check_expiry(session)
                    if session.phase == Phase.EXPIRED:
                        return
                    try:
                        raw, _ = await cloud.request(poll_url, "poll")
                        auth = strict_json(raw.removeprefix(b"&&&START&&&"))
                        del raw
                        break
                    except asyncio.TimeoutError:
                        await asyncio.sleep(1)
                else:
                    raise QRError("session_expired")
                session.image, session.phase = b"", Phase.FETCHING
                stage = "auth_user"
                uid = positive_id(auth["userId"])
                stage = "auth_security"
                security = auth["ssecurity"]
                if not isinstance(security, str) or len(security) > 128 or not 1 <= len(base64.b64decode(security, validate=True)) <= 64:
                    raise QRError("unsupported_protocol")
                stage = "sts_url"
                sts_url = checked_url(auth["location"], "sts")
                stage = "sts_request"
                _, service_token = await cloud.request(sts_url, "sts")
                del sts_url
                del _
                stage = "sts_cookie"
                if not isinstance(service_token, str) or not re.fullmatch(r"[A-Za-z0-9_=.\-+/]{1,4096}", service_token):
                    raise QRError("unsupported_protocol")
                del auth, metadata, poll_url
                stage = "home_list"
                homes = await cloud.api("/app/v2/homeroom/gethome",
                      {"fg": True, "fetch_share": True, "fetch_share_dev": True, "limit": 300, "app_ver": 7}, uid, security, service_token)
                stage = "shared_list"
                shared = await cloud.api("/app/v2/user/get_device_cnt", {"fetch_own": True, "fetch_share": True}, uid, security, service_token)
                stage = "home_ids"
                own, share = homes.get("homelist", []), shared.get("share", {}).get("share_family", [])
                if not isinstance(own, list) or not isinstance(share, list) or len(own) + len(share) > 30:
                    raise QRError("response_limit")
                ids = {(positive_id(home["id"]), uid) for home in own}
                ids.update((positive_id(home["home_id"]), positive_id(home["home_owner"])) for home in share)
                del homes, shared, own, share
                selected, count, account_count = None, 0, 0
                sources = [("/app/home/device_list", {"getVirtualModel": True, "getHuamiDevices": 1,
                            "get_split_device": False, "support_smart_home": True}, "list")]
                sources.extend(("/app/v2/home/home_device_list",
                            {"home_owner": owner_id, "home_id": home_id, "limit": 200,
                             "get_split_device": True, "support_smart_home": True}, "device_info")
                            for home_id, owner_id in sorted(ids))
                for path, parameters, result_key in sources:
                    self.check_expiry(session)
                    if session.phase != Phase.FETCHING or self.closing:
                        return
                    stage = "account_devices" if result_key == "list" else "device_list"
                    devices = await cloud.api(path, parameters, uid, security, service_token)
                    records = devices.get(result_key)
                    if (not isinstance(records, list) or (result_key == "device_info" and len(records) >= 200)
                            or any(key in devices and (type(devices[key]) not in (bool, int) or devices[key] != 0)
                                   for key in ("has_more", "hasmore"))):
                        raise QRError("response_limit")
                    if result_key == "list":
                        account_count = len(records)
                    count += len(records)
                    if count > 1024:
                        raise QRError("response_limit")
                    for device in records:
                        stage = "device_record"
                        if not isinstance(device, dict):
                            raise QRError("unsupported_protocol")
                        token = device.pop("token", None)
                        model = device.get("model")
                        if model is None:
                            model = ""
                        if (not isinstance(model, str) or len(model) > 128
                                or any(unicodedata.category(char).startswith("C") for char in model)):
                            if str(device.get("did")) == TARGET["did"]:
                                raise QRError("target_mismatch")
                            raise QRError("unsupported_protocol")
                        did, name = device.get("did"), device.get("name", "")
                        if (type(did) not in (str, int) or not re.fullmatch(r"[a-zA-Z0-9._:-]{1,128}", str(did))
                                or not isinstance(name, str) or len(name) > 4096):
                            raise QRError("unsupported_protocol")
                        did = str(did)
                        selectable = did == TARGET["did"]
                        if selectable:
                            if (model != TARGET["model"] or device.get("localip") != TARGET["ip"]
                                    or str(device.get("mac", "")).lower() != TARGET["mac"]):
                                raise QRError("target_mismatch")
                            if token is not None and token != "":
                                if (not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", token)
                                        or token.lower() in ("0" * 32, "f" * 32) or selected not in (None, token.lower())):
                                    raise QRError("target_mismatch")
                                selected = token.lower()
                        token = None
                        try:
                            ip = str(ipaddress.IPv4Address(device.get("localip")))
                        except (ValueError, TypeError):
                            ip = None
                        online = device.get("isOnline")
                        item = {"did": did, "name": "".join(char for char in name if not unicodedata.category(char).startswith("C"))[:64].strip() or "未命名设备",
                                "model": model, "ip": ip, "online": online if type(online) is bool else None,
                                "selectable": selectable, "reason": "" if selectable else "unsupported_device"}
                        if did in ready_devices and ready_devices[did] != item:
                            raise QRError("device_conflict")
                        ready_devices[did] = item
                    device, token = None, None
                    del devices, records
                home_count = len(ids)
                del uid, security, service_token, ids
                ready_token = selected
                if TARGET["did"] in ready_devices:
                    ready_devices[TARGET["did"]].update(selectable=bool(selected),
                            reason="" if selected else "credential_unavailable")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            reason = next((code for code in ("unsupported_protocol", "invalid_input", "unsafe_network",
                           "response_limit", "cloud_request_failed", "session_expired", "target_mismatch", "device_conflict")
                           if isinstance(error, QRError) and code == error.code), "cloud_request_failed")
            logger.warning("[qr-diagnostic] stage=%s reason=%s", stage, reason)
            category = next((name for kind, name in ((QRError, "qr_error"), (asyncio.TimeoutError, "timeout"),
                             (aiohttp.ClientError, "client_error"), (OSError, "os_error"))
                             if isinstance(error, kind)), "other")
            if category != "qr_error":
                logger.warning("[qr-diagnostic] stage=%s exception_category=%s", stage, category)
            if session.phase not in (Phase.CANCELED, Phase.EXPIRED):
                session.phase = Phase.FAILED
                session.code = error.code if isinstance(error, QRError) else "cloud_request_failed"
                self.clear(session)
        finally:
            raw = auth = metadata = image = poll_url = sts_url = security = service_token = uid = None
            homes = shared = own = share = devices = records = device = token = selected = _ = None
            if session.phase != Phase.FETCHING or self.closing:
                ready_token = None
            if cloud is not None:
                closing = asyncio.create_task(cloud.close())
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                try:
                    closing.result()
                except Exception:
                    if session.phase not in (Phase.CANCELED, Phase.EXPIRED):
                        session.phase, session.code = Phase.FAILED, "cloud_cleanup_failed"
                        self.clear(session)
        self.check_expiry(session)
        if session.phase == Phase.FETCHING and not self.closing:
            session.token, session.devices, session.phase = ready_token or "", ready_devices, Phase.READY
            session.code = "" if ready_token else ("credential_unavailable" if TARGET["did"] in ready_devices else "target_missing")
            logger.warning("[qr-enumeration] homes=%d records=%d displayed=%d target_found=%d target_visible=%d account_records=%d",
                        home_count, count, len(ready_devices), int(bool(ready_token)), int(TARGET["did"] in ready_devices), account_count)

    def cancel(self, owner, origin):
        session = self.owned(owner, origin)
        if session.phase in (Phase.BINDING, Phase.BOUND):
            raise QRError("binding_already_admitted", 409)
        session.phase, session.code = Phase.CANCELED, "session_canceled"
        self.clear(session)
        return self.public(session)

    async def bind(self, owner, origin, did):
        if not isinstance(did, str) or did != TARGET["did"]:
            raise QRError("invalid_input")
        session = self.owned(owner, origin)
        if session.phase == Phase.BOUND:
            return self.public(session)
        if session.phase == Phase.READY:
            if not session.token or not session.devices.get(did, {}).get("selectable"):
                raise QRError("not_ready", 409)
            session.phase = Phase.QUEUED
            session.binding = asyncio.create_task(self.commit(session, owner, origin, did))
        elif session.phase not in (Phase.QUEUED, Phase.BINDING):
            raise QRError("not_ready", 409)
        await asyncio.shield(session.binding)
        return self.public(session)

    async def commit(self, session, owner, origin, did):
        def admit():
            self.owned(owner, origin)
            if (self.closing or self.session is not session or session.phase != Phase.QUEUED or not session.token
                    or did != TARGET["did"] or not session.devices.get(did, {}).get("selectable")):
                raise QRError("not_ready", 409)
            session.phase = Phase.BINDING
            token = session.token
            self.clear(session)
            return token
        try:
            await self.pair(admit=admit)
            session.phase, session.code = Phase.BOUND, ""
        except Exception:
            if session.phase not in (Phase.CANCELED, Phase.EXPIRED):
                session.phase, session.code = Phase.FAILED, "binding_failed"
        finally:
            self.clear(session)

    async def close(self):
        self.closing = True
        session = self.session
        if session is None:
            return
        if session.phase not in (Phase.BINDING, Phase.BOUND):
            session.phase = Phase.CANCELED
        self.clear(session)
        if session.expiry:
            session.expiry.cancel()
        await asyncio.gather(*[task for task in (session.acquisition, session.expiry, session.binding) if task],
                             return_exceptions=True)
        self.session = None
