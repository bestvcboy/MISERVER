import asyncio
import base64
import json
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import ClientError, ClientOSError
from aiohttp.test_utils import TestClient, TestServer

from qr_connector import (BIND_CONSENT, START_CONSENT, TARGET, Cloud, Phase, PublicResolver,
                          QRConnector, QRError, Session, checked_url, encrypted_fields, rc4, strict_json)
from service import STATE, create_app

OWNER, ORIGIN = "a" * 64, "http://127.0.0.1:8910"
TOKEN = "1234567890abcdef" * 2
SECURITY = base64.b64encode(bytes(range(32))).decode()
STS_URL = "https://sts.api.io.mi.com/sts?" + "&".join(
    key + "=synthetic%2B%2F%3D" for key in
    ("d", "ticket", "pwd", "p_ts", "fid", "p_lm", "auth", "m", "_group", "tsl",
     "p_ca", "p_ur", "p_idc", "nonce", "_ssign"))
SERVICE_COOKIE = base64.b64encode(b"\xfb\xff" * 24).decode()
IMAGE = b"\x89PNG\r\n\x1a\n" + struct.pack("!I", 13) + b"IHDR" + struct.pack("!II", 480, 480) + bytes(9)


class FakeCloud:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.homes = [{"id": 1}]
        self.account_records = []
        self.records = [{"did": "999", "token": "unrelated-secret"},
                        {**TARGET, "localip": TARGET["ip"], "token": TOKEN}]

    async def request(self, url, stage, **kwargs):
        self.calls.append(stage)
        if stage == "create":
            return json.dumps({"timeout": 300,
                "qr": "https://account.xiaomi.com/pass/qr/login?ticket=private&_qrsize=480&sid=xiaomiio",
                "lp": "https://c3.lp.account.xiaomi.com/lp/s?k=private",
                "loginUrl": "https://evil.invalid/never-follow"}).encode(), None
        if stage == "image":
            return IMAGE, None
        if stage == "poll":
            return json.dumps({"userId": "123456", "ssecurity": SECURITY, "passToken": "private-account",
                               "location": "https://sts.api.io.mi.com/sts?sid=xiaomiio&ticket=private"}).encode(), None
        if stage == "sts":
            return b"ok", "private-service-token"
        raise AssertionError(stage)

    async def api(self, path, data, *credentials):
        self.calls.append(path)
        if path == "/app/v2/homeroom/gethome":
            return {"homelist": self.homes}
        if path == "/app/v2/user/get_device_cnt":
            return {"share": {"share_family": []}}
        if path == "/app/home/device_list":
            return {"list": [dict(record) if isinstance(record, dict) else record for record in self.account_records]}
        if path == "/app/v2/home/home_device_list":
            return {"device_info": [dict(record) if isinstance(record, dict) else record for record in self.records]}
        raise AssertionError(path)

    async def close(self):
        self.closed = True


class ProtocolTests(unittest.TestCase):
    def test_url_diagnostics_are_constant_and_keep_invalid_types(self):
        base = "https://sts.api.io.mi.com/sts"
        cases = [("http://sts.api.io.mi.com/sts", "scheme"),
                 (base.replace(".com", ".invalid"), "authority"),
                 (base + "/secret-sentinel", "path"),
                 (base + "#secret-sentinel", "fragment"),
                 (base.replace("https://", "https://secret-sentinel@"), "credentials"),
                 (base + "?secret-sentinel", "query_bare_field"),
                 (base + "?ticket=secret-sentinel&", "query_empty_field"),
                 (base + "?" + "&".join(["ticket=secret-sentinel"] * 32), "query_field_limit"),
                 (base + "?" + "&".join(["ticket=secret-sentinel"] * 13), "query_duplicate"),
                 (base + "?ticket=secret-sentinel&ticket=x", "query_duplicate"),
                 (base + "?secret-sentinel=x", "query_unknown"),
                 (base + "?ticket=" + "x" * 2049, "query_value_length"),
                 (base + "?sid=secret-sentinel", "sid")]
        cases.extend([(base, "stage")])
        for url, reason in cases:
            with self.subTest(reason=reason), self.assertLogs("qr_connector", level="WARNING") as logs:
                with self.assertRaises(QRError) as error:
                    checked_url(url, "secret-sentinel" if reason == "stage" else "sts")
            self.assertEqual((error.exception.code, error.exception.status), ("unsupported_protocol", 400))
            self.assertEqual([r.getMessage() for r in logs.records],
                             ["[qr-diagnostic] stage=checked_url reason=" + reason])
            self.assertTrue(all(r.exc_info is None for r in logs.records))
        for stage, url, reason in (("poll", "https://c3.lp.account.xiaomi.com/lp/s?k=", "poll_key"),
                                   ("image", "https://account.xiaomi.com/pass/qr/login?ticket=secret-sentinel", "image_ticket_size")):
            with self.assertLogs("qr_connector", level="WARNING") as logs, self.assertRaises(QRError):
                checked_url(url, stage)
            self.assertEqual([r.getMessage() for r in logs.records],
                             ["[qr-diagnostic] stage=checked_url reason=" + reason])
        for url in (None, [], 42, b"https://sts.api.io.mi.com/sts"):
            with self.assertRaises(QRError):
                checked_url(url, "sts")
        with self.assertRaises(TypeError):
            checked_url(base, [])

    def test_strict_json_duplicates_constants_and_malformed(self):
        self.assertEqual(strict_json(b'{"a":1}'), {"a": 1})
        for raw in (b'{"a":1,"a":2}', b'{"x":NaN}', b'\xff', b'['):
            with self.assertRaises(QRError):
                strict_json(raw)

    def test_sts_callback_schema_preserves_exact_url(self):
        for url in (STS_URL, STS_URL.replace(".com/", ".com:443/"),
                    STS_URL + "&sid=xiaomiio&clientSign=x&p=x&sign=x&_slh=x"):
            with self.subTest(url=url):
                self.assertEqual(checked_url(url, "sts"), url)

    def test_sts_callback_rejects_invalid_urls(self):
        bad_urls = [STS_URL + suffix for suffix in (
            "&ticket=x", "&%74icket=x", "&unknown=x", "&redirect=https://evil.invalid",
            "&callback=https://evil.invalid", "&sid=other", "&", "&bare", "#fragment")]
        bad_urls += [STS_URL.replace("sts.api.io.mi.com", host) for host in (
            "api.io.mi.com", "account.xiaomi.com", "ak.lp.account.xiaomi.com",
            "127.0.0.1", "sts.api.io.mi.com.evil.invalid", "sts.api.io.mi.com.",
            "STS.api.io.mi.com", "sts.api.io.mi.com:444", "sts.api.io.mi.com:0443",
            "user:pass@sts.api.io.mi.com")]
        bad_urls += [STS_URL.replace("https:", "http:"), STS_URL.replace("/sts?", "/sts/?"),
                     STS_URL.replace("/sts?", "/%73ts?"),
                     STS_URL.replace("d=synthetic%2B%2F%3D", "d=" + "x" * 2049, 1),
                     "https://sts.api.io.mi.com/sts?d=" + "x" * 2048 + "&ticket=" + "x" * 2048]
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(QRError):
                checked_url(url, "sts")

    def test_query_count_caps_and_sts_syntax_diagnostics(self):
        for stage, base in (("create", "https://account.xiaomi.com/longPolling/loginUrl"),
                            ("image", "https://account.xiaomi.com/pass/qr/login"),
                            ("poll", "https://c3.lp.account.xiaomi.com/lp/s"),
                            ("api", "https://api.io.mi.com/app/v2/home/home_device_list"),
                            ("sts", "https://sts.api.io.mi.com/sts")):
            cap = 20 if stage == "sts" else 12
            for count, reason in ((cap, "query_duplicate"), (cap + 1, "query_field_limit")):
                with self.subTest(stage=stage, count=count), self.assertLogs("qr_connector") as logs:
                    with self.assertRaises(QRError):
                        checked_url(base + "?" + "&".join(["ticket=x"] * count), stage)
                self.assertEqual(logs.output, ["WARNING:qr_connector:[qr-diagnostic] stage=checked_url reason=" + reason])
        for suffix, reason in (("&", "query_empty_field"), ("&bare", "query_bare_field")):
            with self.assertLogs("qr_connector") as logs, self.assertRaises(QRError):
                checked_url(STS_URL + suffix, "sts")
            self.assertEqual(logs.output, ["WARNING:qr_connector:[qr-diagnostic] stage=checked_url reason=" + reason])

    def test_closed_url_matrix(self):
        urls = [("image", "https://account.xiaomi.com/pass/qr/login?ticket=x&_qrsize=480&sid=xiaomiio"),
                ("poll", "https://c3.lp.account.xiaomi.com/lp/s?k=x"),
                ("sts", "https://sts.api.io.mi.com/sts?ticket=x&sid=xiaomiio"),
                ("api", "https://api.io.mi.com/app/v2/home/home_device_list")]
        for stage, url in urls:
            self.assertEqual(checked_url(url, stage), url)
            for bad in (url.replace("https:", "http:"), url + "#fragment", url + "&redirect=https://evil.invalid",
                        url.replace(".com", ".com.evil.invalid"), url.replace(".com", ".com:444"),
                        url.replace("https://", "https://user:pass@"), url.replace("/app/", "/app/%2e%2e/")):
                if bad == url:
                    continue
                with self.assertRaises(QRError, msg=bad):
                    checked_url(bad, stage)
        for url in ("https://c4.lp.account.xiaomi.com/lp/s?k=x", "https://c3.lp.account.xiaomi.com/lp/s?k=x&k=y",
                    "https://127.0.0.1/lp/s?k=x", "https://c3.lp.account.xiaomi.com/longPolling/login?k=x"):
            with self.assertRaises(QRError):
                checked_url(url, "poll")

    def test_ak_poll_url_exact_host_and_query(self):
        for authority in ("ak.lp.account.xiaomi.com", "ak.lp.account.xiaomi.com:443"):
            url = "https://" + authority + "/lp/s?k=test-only"
            with self.subTest(url=url):
                self.assertEqual(checked_url(url, "poll"), url)
        for url in ("http://ak.lp.account.xiaomi.com/lp/s?k=x",
                    "https://ak.lp.account.xiaomi.com:444/lp/s?k=x",
                    "https://ak.lp.account.xiaomi.com:0443/lp/s?k=x",
                    "https://ak.lp.account.xiaomi.com.evil.invalid/lp/s?k=x",
                    "https://ak.lp.account.xiaomi.com./lp/s?k=x",
                    "https://AK.lp.account.xiaomi.com/lp/s?k=x",
                    "https://ak.lp.account.xiaomi.com/longPolling/login?k=x",
                    "https://ak.lp.account.xiaomi.com/lp/s/?k=x",
                    "https://ak.lp.account.xiaomi.com/lp/%73?k=x",
                    "https://ak.lp.account.xiaomi.com/lp/s?k=x&k=y",
                    "https://ak.lp.account.xiaomi.com/lp/s?k=x&%6b=y",
                    "https://ak.lp.account.xiaomi.com/lp/s",
                    "https://ak.lp.account.xiaomi.com/lp/s?k=",
                    "https://ak.lp.account.xiaomi.com/lp/s?k",
                    "https://ak.lp.account.xiaomi.com/lp/s?other=x",
                    "https://ak.lp.account.xiaomi.com/lp/s?k=x&other=y",
                    "https://ak.lp.account.xiaomi.com/lp/s?k=x#fragment",
                    "https://user:pass@ak.lp.account.xiaomi.com/lp/s?k=x"):
            with self.subTest(url=url), self.assertRaises(QRError) as error:
                checked_url(url, "poll")
            self.assertEqual(error.exception.code, "unsupported_protocol")

    def test_crypto_roundtrip_and_signing_fields(self):
        nonce = base64.b64encode(bytes(range(12))).decode()
        key, fields = encrypted_fields("/app/v2/home/home_device_list", SECURITY, nonce, {"home_id": 1})
        self.assertEqual(set(fields), {"data", "rc4_hash__", "signature", "ssecurity", "_nonce"})
        self.assertEqual(fields["_nonce"], nonce)
        self.assertEqual(rc4(key, base64.b64decode(fields["data"])), b'{"home_id":1}')
        self.assertEqual(len(base64.b64decode(fields["signature"])), 20)
        self.assertEqual(key, "Yw0l0FbWPFMOrBM72uKLsAFvboGzGR32VPkQ6+9wunc=")
        self.assertEqual(fields["data"], "ZMXxwiGLGJxAs4RKLQ==")
        self.assertEqual(fields["rc4_hash__"], "KJPP42e2Lq0Q8PhDFWXipct8mfr00HvdbFRdsw==")
        self.assertEqual(fields["signature"], "XbgKOwVbdsS4zesUWMANO6sOD94=")


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_dns_answers_refused_at_connect(self):
        loop = asyncio.get_running_loop()
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.3.180", 443))
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[public])):
            self.assertEqual((await PublicResolver().resolve("api.io.mi.com", 443))[0]["host"], "8.8.8.8")
        for records in ([], [private], [public, private]):
            with patch.object(loop, "getaddrinfo", AsyncMock(return_value=records)), self.assertRaises(QRError):
                await PublicResolver().resolve("api.io.mi.com", 443)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_compression_body_budget_and_non_global_dns_are_closed(self):
        class Content:
            async def iter_chunked(self, size):
                for chunk in chunks:
                    yield chunk
        class Response:
            status = 200
            headers = {}
            cookies = {}
            content = Content()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        cloud = Cloud()
        response = Response()
        url = "https://c3.lp.account.xiaomi.com/lp/s?k=test-only"
        try:
            chunks = [b'{}']
            with patch.object(cloud.session, "request", return_value=response) as request:
                self.assertEqual((await cloud.request(url, "poll"))[0], b'{}')
                self.assertFalse(request.call_args.kwargs["allow_redirects"])
                self.assertEqual(request.call_args.kwargs["timeout"].total, 12)
                for status, headers, chunk_list in ((302, {"Location": "https://secret-sentinel.invalid"}, [b'secret-sentinel']),
                                                   (503, {"Set-Cookie": "secret-sentinel"}, [b'secret-sentinel']),
                                                   (200, {"Content-Encoding": "secret-sentinel"}, [b'']),
                                                   (200, {}, [b'x' * 1048576] * 3)):
                    response.status, response.headers, chunks = status, headers, chunk_list
                    if headers:
                        with self.assertLogs("qr_connector", level="WARNING") as logs, self.assertRaises(QRError):
                            await cloud.request(url, "poll")
                        self.assertEqual([r.getMessage() for r in logs.records],
                                         ["[qr-diagnostic] stage=poll reason=encoding" if status == 200 else
                                          "[qr-diagnostic] stage=poll reason=http_status status=" + str(status)])
                        self.assertNotIn("secret-sentinel", str([(r.msg, r.args, r.exc_info) for r in logs.records]))
                        self.assertTrue(all(r.exc_info is None for r in logs.records))
                    else:
                        with self.assertRaises(QRError):
                            await cloud.request(url, "poll")
                response.status, response.headers, chunks = 200, {}, [b'{}']
                cloud.requests = 64
                with self.assertRaises(QRError):
                    await cloud.request(url, "poll")
                cloud.requests, cloud.bytes = 0, 8388608
                with self.assertRaises(QRError):
                    await cloud.request(url, "poll")
        finally:
            await cloud.close()


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_sts_callback_and_base64_cookie_acquire_without_binding(self):
        request = self.cloud.request
        async def response(url, stage, **kwargs):
            raw, cookie = await request(url, stage, **kwargs)
            if stage == "poll":
                auth = json.loads(raw)
                auth["location"] = STS_URL
                raw = json.dumps(auth).encode()
            if stage == "sts":
                self.assertEqual(url, STS_URL)
                cookie = SERVICE_COOKIE
            return raw, cookie
        self.cloud.request = response
        with patch.object(self.cloud, "api", wraps=self.cloud.api) as api:
            session = await self.ready()
        self.assertEqual(session.token, TOKEN)
        self.assertIsNone(session.binding)
        self.assertTrue(self.cloud.closed)
        self.assertEqual(self.cloud.calls[:4], ["create", "image", "poll", "sts"])
        self.assertEqual(api.call_count, 4)
        for call in api.call_args_list:
            self.assertEqual(call.args[-1], SERVICE_COOKIE)
        self.pair.assert_not_called()
        self.assertNotIn(SERVICE_COOKIE, json.dumps(self.connector.public(session)) + repr(session))

    async def test_sts_cookie_character_and_length_boundaries(self):
        for cookie in (SERVICE_COOKIE, "x", "x" * 4096, "old_token=.token-", "", "x" * 4097,
                       None, b"token", "x y", "x;y", "x!y", "x\x00y", "x\ty", "x\ry", "x\ny"):
            with self.subTest(cookie=cookie):
                cloud = FakeCloud()
                request = cloud.request
                async def response(url, stage, **kwargs):
                    raw, original = await request(url, stage, **kwargs)
                    return raw, cookie if stage == "sts" else original
                cloud.request = response
                connector = QRConnector(self.pair, lambda: cloud)
                try:
                    connector.start(OWNER, ORIGIN)
                    await connector.session.acquisition
                    accepted = cookie in (SERVICE_COOKIE, "x", "x" * 4096, "old_token=.token-")
                    self.assertEqual(connector.session.phase, Phase.READY if accepted else Phase.FAILED)
                    self.assertEqual(connector.session.token, TOKEN if accepted else "")
                    self.assertIsNone(connector.session.binding)
                    self.assertTrue(cloud.closed)
                    if not accepted:
                        self.assertEqual(cloud.calls, ["create", "image", "poll", "sts"])
                        self.assertEqual(connector.session.code, "unsupported_protocol")
                    self.pair.assert_not_called()
                finally:
                    await connector.close()

    async def test_after_scan_diagnostics_are_secret_free_and_do_not_bind(self):
        for failure, reason in (("sts_url", "unsupported_protocol"),
                                ("sts_cookie", "unsupported_protocol"),
                                ("auth_security", "unsupported_protocol"),
                                ("exception", "cloud_request_failed"),
                                ("client", "cloud_request_failed"),
                                ("client_os", "cloud_request_failed"),
                                ("os", "cloud_request_failed"),
                                ("error_code", "cloud_request_failed")):
            with self.subTest(failure=failure):
                cloud = FakeCloud()
                request = cloud.request
                async def response(url, stage, **kwargs):
                    raw, cookie = await request(url, stage, **kwargs)
                    if stage == "poll":
                        if failure == "exception":
                            raise ValueError("secret-sentinel")
                        if failure in ("client", "client_os", "os"):
                            raise {"client": ClientError, "client_os": ClientOSError,
                                   "os": OSError}[failure]("secret-sentinel")
                        if failure == "error_code":
                            raise QRError("secret-sentinel")
                        auth = json.loads(raw)
                        auth["passToken"] = "secret-sentinel"
                        if failure == "sts_url":
                            auth["location"] = "https://secret-sentinel.invalid/sts?ticket=secret-sentinel"
                        if failure == "auth_security":
                            auth["ssecurity"] = "secret-sentinel" * 10
                        raw = json.dumps(auth).encode()
                    if stage == "sts" and failure == "sts_cookie":
                        cookie = "secret-sentinel!"
                    return raw, cookie
                cloud.request = response
                connector = QRConnector(self.pair, lambda: cloud)
                try:
                    with self.assertLogs("qr_connector", level="WARNING") as logs:
                        connector.start(OWNER, ORIGIN)
                        await connector.session.acquisition
                    stage = "poll" if failure in ("exception", "client", "client_os", "os", "error_code") else failure
                    expected = ["[qr-diagnostic] stage=" + stage + " reason=" + reason]
                    if failure == "sts_url":
                        expected.insert(0, "[qr-diagnostic] stage=checked_url reason=authority")
                    if failure in ("exception", "client", "client_os", "os"):
                        category = {"exception": "other", "client": "client_error",
                                    "client_os": "client_error", "os": "os_error"}[failure]
                        expected.append("[qr-diagnostic] stage=poll exception_category=" + category)
                    self.assertEqual([r.getMessage() for r in logs.records], expected)
                    for secret in ("secret-sentinel", TOKEN, OWNER, SECURITY, "private-account", "private-service-token"):
                        self.assertNotIn(secret, str([(r.msg, r.args, r.exc_info) for r in logs.records]))
                    self.assertTrue(all(r.exc_info is None for r in logs.records))
                    self.assertEqual(connector.session.phase, Phase.FAILED)
                    self.assertEqual(connector.session.token, "")
                    self.assertEqual(connector.session.image, b"")
                    self.assertIsNone(connector.session.binding)
                    self.assertTrue(cloud.closed)
                    self.pair.assert_not_called()
                finally:
                    await connector.close()

    async def test_outer_timeout_has_fixed_category(self):
        request = self.cloud.request
        async def response(url, stage, **kwargs):
            if stage == "poll":
                await asyncio.Future()
            return await request(url, stage, **kwargs)
        timeout = asyncio.timeout
        with patch.object(self.cloud, "request", side_effect=response), \
                patch("qr_connector.asyncio.timeout", side_effect=lambda seconds: timeout(0.01)), \
                self.assertLogs("qr_connector", level="WARNING") as logs:
            self.connector.start(OWNER, ORIGIN)
            await self.connector.session.acquisition
        self.assertEqual([r.getMessage() for r in logs.records], [
            "[qr-diagnostic] stage=poll reason=cloud_request_failed",
            "[qr-diagnostic] stage=poll exception_category=timeout"])
        self.assertTrue(all(r.exc_info is None for r in logs.records))
        self.assertEqual(self.connector.session.phase, Phase.FAILED)
        self.assertEqual(self.connector.session.image, b"")
        self.assertTrue(self.cloud.closed)
        self.pair.assert_not_called()

    async def asyncSetUp(self):
        self.cloud = FakeCloud()
        self.pair = AsyncMock()
        self.connector = QRConnector(self.pair, lambda: self.cloud)

    async def asyncTearDown(self):
        await self.connector.close()

    async def ready(self):
        self.connector.start(OWNER, ORIGIN)
        await self.connector.session.acquisition
        self.assertEqual(self.connector.session.phase, Phase.READY)
        return self.connector.session

    async def test_acquire_fixed_token_without_public_secrets_and_no_autobind(self):
        first = self.connector.start(OWNER, ORIGIN)
        self.assertEqual(first["phase"], "preparing")
        self.connector.start(OWNER, ORIGIN)
        await self.connector.session.acquisition
        session = self.connector.session
        self.assertTrue(self.cloud.closed)
        self.assertEqual(session.token, TOKEN)
        self.assertEqual(session.phase, Phase.READY)
        self.pair.assert_not_called()
        serialized = json.dumps(self.connector.public(session)) + repr(session)
        for secret in (TOKEN, OWNER, "private-account", "private-service-token", "unrelated-secret", SECURITY):
            self.assertNotIn(secret, serialized)
        self.assertEqual(self.cloud.calls.count("create"), 1)

    async def test_foreign_owner_and_origin_cannot_take_over(self):
        await self.ready()
        for owner, origin in (("b" * 64, ORIGIN), (OWNER, "https://other.invalid")):
            with self.assertRaises(QRError):
                self.connector.start(owner, origin)
            with self.assertRaises(QRError):
                self.connector.cancel(owner, origin)
        self.assertEqual(self.connector.session.token, TOKEN)

    async def test_all_returned_models_and_missing_metadata_keep_target_visible(self):
        self.cloud.records[0].update(name="\x00 ", localip="192.168.3.25")
        self.cloud.records += [
            {"did": "speaker", "model": "xiaomi.wifispeaker.lx06", "name": "音箱", "token": "speaker-secret"},
            {"did": "sensor", "model": "lumi.sensor_ht.agl01", "token": "sensor-secret"},
            {"did": "empty", "model": "", "localip": "bad-ip", "token": "empty-secret"},
            {"did": "null", "model": None, "token": "null-secret"},
            {"did": "generic", "model": "普通设备型号", "token": "generic-secret"}]
        session = await self.ready()
        public = self.connector.public(session)
        self.assertEqual([device["did"] for device in public["devices"]],
                         ["999", TARGET["did"], "speaker", "sensor", "empty", "null", "generic"])
        self.assertEqual(public["devices"][0], {"did": "999", "name": "未命名设备", "model": "",
            "ip": "192.168.3.25", "online": None, "selectable": False, "reason": "unsupported_device"})
        self.assertEqual(public["devices"][4]["model"], "")
        self.assertIsNone(public["devices"][4]["ip"])
        self.assertEqual(public["devices"][5]["model"], "")
        self.assertEqual(public["devices"][6]["model"], "普通设备型号")
        for device in public["devices"]:
            self.assertEqual(set(device), {"did", "name", "model", "ip", "online", "selectable", "reason"})
            self.assertEqual(device["selectable"], device["did"] == TARGET["did"])
        self.assertEqual(session.token, TOKEN)
        self.assertEqual(public["code"], "")
        for secret in (TOKEN, OWNER, SECURITY, "unrelated-secret", "speaker-secret", "sensor-secret", "empty-secret",
                       "null-secret", "generic-secret", "private-account", "private-service-token"):
            self.assertNotIn(secret, json.dumps(public) + repr(session))
        self.pair.assert_not_called()

    async def test_enumeration_log_contains_only_numeric_counts(self):
        for target_found in (True, False):
            with self.subTest(target_found=target_found):
                cloud = FakeCloud()
                if not target_found:
                    cloud.records.pop()
                cloud.records.append(dict(cloud.records[0]))
                connector = QRConnector(self.pair, lambda: cloud)
                try:
                    with self.assertLogs("qr_connector", level="INFO") as logs:
                        connector.start(OWNER, ORIGIN)
                        await connector.session.acquisition
                    self.assertEqual([record.getMessage() for record in logs.records], [
                        "[qr-enumeration] homes=1 records=%d displayed=%d target_found=%d target_visible=%d account_records=0"
                        % (len(cloud.records), len(cloud.records) - 1, int(target_found), int(target_found))])
                    for record in logs.records:
                        self.assertEqual(record.levelname, "WARNING")
                        self.assertTrue(all(type(value) is int for value in record.args))
                        self.assertIsNone(record.exc_info)
                    for secret in (TOKEN, OWNER, SECURITY, TARGET["did"], TARGET["ip"],
                                   "unrelated-secret", "private-account", "private-service-token"):
                        self.assertNotIn(secret, repr([(r.msg, r.args, r.exc_info) for r in logs.records]))
                finally:
                    await connector.close()

    async def test_invalid_models_fail_validation_and_fixed_missing_model_mismatches(self):
        for model in (True, 123, [], {}, "bad\x00model", "bad\u202emodel", "x" * 129):
            with self.subTest(model=model):
                cloud = FakeCloud()
                cloud.records[0]["model"] = model
                connector = QRConnector(self.pair, lambda: cloud)
                try:
                    connector.start(OWNER, ORIGIN)
                    await connector.session.acquisition
                    self.assertEqual(connector.session.phase, Phase.FAILED)
                    self.assertEqual(connector.session.code, "unsupported_protocol")
                    self.assertEqual(connector.session.token, "")
                    self.assertEqual(connector.public(connector.session)["devices"], [])
                finally:
                    await connector.close()
        for metadata in ({}, {"model": None}, {"model": ""}):
            cloud = FakeCloud()
            cloud.records[1].pop("model")
            cloud.records[1].update(metadata)
            connector = QRConnector(self.pair, lambda: cloud)
            try:
                connector.start(OWNER, ORIGIN)
                await connector.session.acquisition
                self.assertEqual(connector.session.code, "target_mismatch")
                self.assertEqual(connector.session.phase, Phase.FAILED)
                self.assertEqual(connector.session.token, "")
            finally:
                await connector.close()
        self.pair.assert_not_called()

    async def test_cancel_and_expiry_clear_ready(self):
        session = await self.ready()
        self.connector.cancel(OWNER, ORIGIN)
        self.assertEqual(session.token, "")
        self.assertEqual(session.phase, Phase.CANCELED)
        with self.assertRaises(QRError):
            await self.connector.bind(OWNER, ORIGIN, TARGET["did"])
        await asyncio.gather(session.acquisition, session.expiry, return_exceptions=True)
        self.connector.start("b" * 64, ORIGIN)
        await self.connector.session.acquisition
        session = self.connector.session
        session.deadline = time.monotonic() - 1
        self.assertEqual(self.connector.public(session)["phase"], "expired")
        self.assertEqual(session.token, "")

    async def test_ready_is_not_published_until_cloud_cleanup_and_cancel_retains_ownership(self):
        closing, release = asyncio.Event(), asyncio.Event()
        async def close():
            closing.set()
            await release.wait()
            self.cloud.closed = True
        self.cloud.close = close
        self.connector.start(OWNER, ORIGIN)
        session = self.connector.session
        await closing.wait()
        self.assertEqual(session.phase, Phase.FETCHING)
        self.assertEqual(session.token, "")
        self.connector.cancel(OWNER, ORIGIN)
        with self.assertRaises(QRError):
            self.connector.start("b" * 64, ORIGIN)
        await asyncio.sleep(0)
        self.assertFalse(session.acquisition.done())
        release.set()
        await asyncio.gather(session.acquisition, session.expiry, return_exceptions=True)
        self.assertTrue(self.cloud.closed)
        self.assertEqual(session.phase, Phase.CANCELED)
        self.assertEqual(session.token, "")
        self.connector.start("b" * 64, ORIGIN)
        await self.connector.session.acquisition
        self.assertEqual(self.connector.session.phase, Phase.READY)

    async def test_source_compatible_16_byte_security_is_accepted(self):
        request = self.cloud.request
        async def response(url, stage, **kwargs):
            raw, cookie = await request(url, stage, **kwargs)
            if stage == "poll":
                auth = json.loads(raw)
                auth["ssecurity"] = base64.b64encode(bytes(range(16))).decode()
                raw = json.dumps(auth).encode()
            return raw, cookie
        self.cloud.request = response
        await self.ready()

    async def test_error_final_cleanup_drains_first_cancellation_and_shutdown(self):
        for shutdown in (False, True):
            cloud = FakeCloud()
            closing, release = asyncio.Event(), asyncio.Event()
            async def failed_request(*args, **kwargs):
                raise QRError("cloud_request_failed")
            async def close():
                closing.set()
                await release.wait()
                cloud.closed = True
            cloud.request, cloud.close = failed_request, close
            connector = QRConnector(self.pair, lambda: cloud)
            connector.start(OWNER, ORIGIN)
            session = connector.session
            await closing.wait()
            self.assertEqual(session.phase, Phase.FAILED)
            if shutdown:
                pending_close = asyncio.create_task(connector.close())
            else:
                connector.cancel(OWNER, ORIGIN)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(session.acquisition.done())
            with self.assertRaises(QRError):
                connector.start("b" * 64, ORIGIN)
            release.set()
            await asyncio.gather(session.acquisition, session.expiry, return_exceptions=True)
            self.assertTrue(cloud.closed)
            self.assertEqual(session.token, "")
            if shutdown:
                await pending_close
            else:
                await connector.close()

    async def test_admitted_bind_is_idempotent_and_not_cancelable(self):
        session = await self.ready()
        admitted, release = asyncio.Event(), asyncio.Event()
        async def pair(*, admit):
            self.assertEqual(admit(), TOKEN)
            admitted.set()
            await release.wait()
        self.connector.pair = pair
        first = asyncio.create_task(self.connector.bind(OWNER, ORIGIN, TARGET["did"]))
        await admitted.wait()
        self.assertEqual(session.phase, Phase.BINDING)
        with self.assertRaises(QRError):
            self.connector.cancel(OWNER, ORIGIN)
        second = asyncio.create_task(self.connector.bind(OWNER, ORIGIN, TARGET["did"]))
        release.set()
        self.assertEqual((await first)["phase"], "bound")
        self.assertEqual((await second)["phase"], "bound")
        self.assertEqual(session.token, "")

    async def test_shutdown_drains_admitted_binding(self):
        await self.ready()
        admitted, release = asyncio.Event(), asyncio.Event()
        async def pair(*, admit):
            admit()
            admitted.set()
            await release.wait()
        self.connector.pair = pair
        binding = asyncio.create_task(self.connector.bind(OWNER, ORIGIN, TARGET["did"]))
        await admitted.wait()
        closing = asyncio.create_task(self.connector.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await binding
        await closing
        self.assertIsNone(self.connector.session)

    async def test_wrong_identity_and_conflicting_token_fail(self):
        for records in ([{**TARGET, "localip": TARGET["ip"], "token": TOKEN, "mac": "wrong"}],
                        [{**TARGET, "localip": TARGET["ip"], "token": TOKEN},
                         {**TARGET, "localip": TARGET["ip"], "token": "abcd" * 8}],
                        [{**TARGET, "localip": TARGET["ip"], "token": "f" * 32}]):
            cloud = FakeCloud()
            cloud.records = records
            connector = QRConnector(self.pair, lambda: cloud)
            connector.start(OWNER, ORIGIN)
            await connector.session.acquisition
            self.assertEqual(connector.session.phase, Phase.FAILED)
            self.assertEqual(connector.session.token, "")
            self.assertTrue(cloud.closed)
            await connector.close()
        self.pair.assert_not_called()

    async def test_enumeration_ready_without_target_and_allowlisted_sanitized_metadata(self):
        self.cloud.records = [
            {"did": "999", "model": "zhimi.airpurifier.mb4", "name": "\x00\u202e<script>" + "净" * 100,
             "localip": "192.168.3.25", "isOnline": False, "token": "unsupported-secret", "uid": "private-uid"},
            {"did": "888", "model": "xiaomi.light.v1", "token": "light-secret"}]
        session = await self.ready()
        session.devices["999"]["token"] = "injected-secret"
        public = self.connector.public(session)
        self.assertNotIn("injected-secret", json.dumps(public))
        self.assertEqual(session.phase, Phase.READY)
        self.assertEqual(session.token, "")
        self.assertEqual(public["code"], "target_missing")
        self.assertEqual(len(public["devices"]), 2)
        self.assertEqual(public["devices"][1], {"did": "888", "name": "未命名设备",
            "model": "xiaomi.light.v1", "ip": None, "online": None,
            "selectable": False, "reason": "unsupported_device"})
        device = public["devices"][0]
        self.assertEqual(set(device), {"did", "name", "model", "ip", "online", "selectable", "reason"})
        self.assertEqual(device["did"], "999")
        self.assertEqual(device["name"], ("<script>" + "净" * 100)[:64])
        self.assertEqual(device["reason"], "unsupported_device")
        self.assertFalse(device["selectable"])
        self.assertIs(device["online"], False)
        for secret in ("unsupported-secret", "light-secret", "private-uid", SECURITY, OWNER):
            self.assertNotIn(secret, json.dumps(public) + repr(session))
        with self.assertRaises(QRError):
            await self.connector.bind(OWNER, ORIGIN, "999")
        with self.assertRaises(QRError):
            await self.connector.bind(OWNER, ORIGIN, TARGET["did"])
        self.pair.assert_not_called()

    async def test_empty_enumeration_ready(self):
        self.cloud.records = []
        session = await self.ready()
        self.assertEqual(self.connector.public(session)["devices"], [])
        self.assertEqual(session.phase, Phase.READY)
        self.assertEqual(session.token, "")

    async def test_duplicate_metadata_conflicts_and_bounds(self):
        base = {"did": "999", "model": "zhimi.airpurifier.mb4", "name": "other",
                "localip": "192.168.3.25", "token": "discard-me"}
        for records, phase in (([base, dict(base)], Phase.READY),
                               ([base, {**base, "localip": "192.168.3.26"}], Phase.FAILED),
                               ([{**base, "did": []}], Phase.FAILED),
                               ([{**base, "name": {}}], Phase.FAILED),
                               ([base] * 200, Phase.FAILED)):
            cloud = FakeCloud()
            cloud.records = records
            connector = QRConnector(self.pair, lambda: cloud)
            connector.start(OWNER, ORIGIN)
            await connector.session.acquisition
            self.assertEqual(connector.session.phase, phase)
            self.assertEqual(connector.session.token, "")
            self.assertNotIn("discard-me", repr(connector.session))
            await connector.close()

    async def test_exact_did_and_single_retry_after_credentials_cleared(self):
        session = await self.ready()
        calls = []
        admitted, release = asyncio.Event(), asyncio.Event()
        async def pair(*, admit):
            calls.append(admit())
            admitted.set()
            await release.wait()
        self.connector.pair = pair
        for did in (None, True, 70632677, "", "999", [], {}):
            with self.assertRaises(QRError):
                await self.connector.bind(OWNER, ORIGIN, did)
        first = asyncio.create_task(self.connector.bind(OWNER, ORIGIN, TARGET["did"]))
        await admitted.wait()
        self.assertEqual(session.token, "")
        self.assertEqual(session.devices, {})
        with self.assertRaises(QRError):
            await self.connector.bind(OWNER, ORIGIN, "999")
        retry = asyncio.create_task(self.connector.bind(OWNER, ORIGIN, TARGET["did"]))
        release.set()
        self.assertEqual((await first)["phase"], "bound")
        self.assertEqual((await retry)["phase"], "bound")
        self.assertEqual((await self.connector.bind(OWNER, ORIGIN, TARGET["did"]))["phase"], "bound")
        with self.assertRaises(QRError):
            await self.connector.bind(OWNER, ORIGIN, "999")
        self.assertEqual(calls, [TOKEN])

    async def test_fresh_admission_rechecks_candidate(self):
        session = await self.ready()
        gate = asyncio.Event()
        async def pair(*, admit):
            await gate.wait()
            admit()
        self.connector.pair = pair
        pending = asyncio.create_task(self.connector.bind(OWNER, ORIGIN, TARGET["did"]))
        await asyncio.sleep(0)
        session.devices.clear()
        gate.set()
        self.assertEqual((await pending)["phase"], "failed")
        self.assertEqual(session.token, "")


class SharedDevicesTests(unittest.IsolatedAsyncioTestCase):
    async def acquire(self, cloud):
        pair = AsyncMock()
        connector = QRConnector(pair, lambda: cloud)
        self.addAsyncCleanup(connector.close)
        connector.start(OWNER, ORIGIN)
        await connector.session.acquisition
        pair.assert_not_called()
        public = connector.public(connector.session)
        for secret in (TOKEN, SECURITY, OWNER, "private-account", "private-service-token", "unrelated-secret"):
            self.assertNotIn(secret, json.dumps(public) + repr(connector.session))
        self.assertTrue(cloud.closed)
        return connector, public

    async def test_account_only_target_without_homes_exact_requests(self):
        cloud = FakeCloud()
        cloud.homes = []
        cloud.account_records = cloud.records
        with patch.object(cloud, "api", wraps=cloud.api) as api:
            connector, public = await self.acquire(cloud)
        self.assertEqual(public["phase"], "ready")
        self.assertEqual(connector.session.token, TOKEN)
        self.assertEqual(public["devices"][1], {"did": TARGET["did"], "name": "未命名设备",
            "model": TARGET["model"], "ip": TARGET["ip"], "online": None, "selectable": True, "reason": ""})
        self.assertEqual([call.args[0] for call in api.call_args_list], [
            "/app/v2/homeroom/gethome", "/app/v2/user/get_device_cnt", "/app/home/device_list"])
        self.assertEqual(api.call_args_list[-1].args, ("/app/home/device_list",
            {"getVirtualModel": True, "getHuamiDevices": 1, "get_split_device": False, "support_smart_home": True},
            123456, SECURITY, "private-service-token"))

    async def test_account_api_uses_existing_encrypted_transport_and_exact_allowlist(self):
        path = "/app/home/device_list"
        url = "https://api.io.mi.com" + path
        self.assertEqual(checked_url(url, "api"), url)
        for invalid in (url + "/", url + "?limit=200", url.replace("home/device_list", "v2/home/device_list_page")):
            with self.assertRaises(QRError):
                checked_url(invalid, "api")
        cloud = Cloud()
        self.addAsyncCleanup(cloud.close)
        params = {"getVirtualModel": True, "getHuamiDevices": 1, "get_split_device": False, "support_smart_home": True}
        async def request(actual_url, stage, *, data, cookies):
            self.assertEqual((actual_url, stage), (url, "api"))
            key, expected = encrypted_fields(path, SECURITY, data["_nonce"], params)
            self.assertEqual(data, expected)
            self.assertEqual(json.loads(rc4(key, base64.b64decode(data["data"]))), params)
            self.assertEqual(cookies["serviceToken"], SERVICE_COOKIE)
            return base64.b64encode(rc4(key, b'{"code":0,"result":{"list":[]}}')), None
        with patch.object(cloud, "request", side_effect=request):
            self.assertEqual(await cloud.api(path, params, 123456, SECURITY, SERVICE_COOKIE), {"list": []})

    async def test_missing_credentials_visible_and_duplicate_credentials_order_independent(self):
        target = {**TARGET, "localip": TARGET["ip"]}
        for missing in ({}, {"token": None}, {"token": ""}):
            for tokens in ((missing, None), (missing, missing), (missing, {"token": TOKEN}), ({"token": TOKEN}, missing),
                           ({"token": TOKEN.upper()}, {"token": TOKEN})):
                with self.subTest(tokens=tokens):
                    cloud = FakeCloud()
                    cloud.account_records = [{**target, **tokens[0]}]
                    cloud.records = [] if tokens[1] is None else [{**target, **tokens[1]}]
                    connector, public = await self.acquire(cloud)
                    usable = any(value and value.get("token") for value in tokens)
                    self.assertEqual(public["phase"], "ready")
                    self.assertEqual(len(public["devices"]), 1)
                    row = public["devices"][0]
                    self.assertEqual(set(row), {"did", "name", "model", "ip", "online", "selectable", "reason"})
                    self.assertEqual(row["selectable"], bool(usable))
                    self.assertEqual(row["reason"], "" if usable else "credential_unavailable")
                    self.assertEqual(public["code"], row["reason"])
                    self.assertEqual(connector.session.token, TOKEN if usable else "")
                    if not usable:
                        with self.assertRaises(QRError):
                            await connector.bind(OWNER, ORIGIN, TARGET["did"])
                        connector.pair.assert_not_called()

    async def test_each_occurrence_requires_identity_and_strict_token_and_metadata(self):
        target = {**TARGET, "localip": TARGET["ip"], "token": TOKEN}
        invalid = [{"token": value} for value in (False, 0, [], {}, " ", "bad", "0" * 32, "f" * 32, "abcd" * 8)]
        invalid += [{"model": ""}, {"localip": None}, {"mac": "wrong"}, {"mac": None}]
        for change in invalid + [{"name": "changed"}, {"isOnline": True}]:
            for reverse in (False, True):
                with self.subTest(change=change, reverse=reverse):
                    cloud = FakeCloud()
                    rows = [target, {**target, **change}]
                    if reverse:
                        rows.reverse()
                    cloud.account_records, cloud.records = rows[:1], rows[1:]
                    connector, public = await self.acquire(cloud)
                    self.assertEqual(public["phase"], "failed")
                    self.assertEqual(public["code"], "device_conflict" if "name" in change or "isOnline" in change else "target_mismatch")
                    self.assertEqual(public["devices"], [])
                    self.assertEqual(connector.session.token, "")

    async def test_lists_flags_limits_and_failures_never_publish_partial_result(self):
        for path, key in (("/app/home/device_list", "list"), ("/app/v2/home/home_device_list", "device_info")):
            invalid = [{}]
            invalid += [{key: value} for value in (None, False, 0, "", {}, [None])]
            invalid += [{key: [], flag: value} for flag in ("has_more", "hasmore")
                        for value in (True, 1, -1, "0", [], {}, None, 0.0)]
            invalid += [{key: [{"did": "999"}] * (1025 if key == "list" else 200)}, RuntimeError("private-account")]
            for result in invalid:
                with self.subTest(path=path, result=type(result).__name__):
                    cloud = FakeCloud()
                    cloud.account_records = cloud.records
                    original = cloud.api
                    async def api(actual_path, *args):
                        if actual_path == path:
                            if isinstance(result, Exception):
                                raise result
                            return result
                        return await original(actual_path, *args)
                    cloud.api = api
                    with self.assertLogs("qr_connector", level="WARNING") as logs:
                        connector, public = await self.acquire(cloud)
                    self.assertEqual(public["phase"], "failed")
                    self.assertEqual(public["devices"], [])
                    self.assertEqual(connector.session.token, "")
                    self.assertNotIn("private-account", " ".join(logs.output))
        for total in (1024, 1025):
            cloud = FakeCloud()
            cloud.account_records = [{"did": "999"}] * 1024
            cloud.records = [{"did": "999"}] * (total - 1024)
            _, public = await self.acquire(cloud)
            self.assertEqual(public["phase"], "ready" if total == 1024 else "failed")
            if total == 1024:
                self.assertEqual(len(public["devices"]), 1)

    async def test_cancel_expiry_and_failure_drop_auth_before_cleanup(self):
        for outcome in ("cancel", "expire", "fail", "success"):
            cloud = FakeCloud()
            cloud.account_records = cloud.records
            pair = AsyncMock()
            connector = QRConnector(pair, lambda: cloud)
            self.addAsyncCleanup(connector.close)
            original = cloud.api
            async def api(path, *args):
                if path == "/app/home/device_list":
                    if outcome == "cancel":
                        connector.cancel(OWNER, ORIGIN)
                        await asyncio.sleep(0)
                    if outcome == "expire":
                        connector.session.deadline = 0
                    if outcome == "fail":
                        raise QRError("cloud_request_failed")
                return await original(path, *args)
            async def close():
                frame = connector.session.acquisition.get_stack()[0]
                for name in ("raw", "auth", "metadata", "security", "service_token", "uid", "devices", "records", "device", "token", "selected"):
                    self.assertIsNone(frame.f_locals[name], name)
                self.assertEqual(connector.session.devices, {})
                self.assertEqual(connector.session.token, "")
                cloud.closed = True
            cloud.api, cloud.close = api, close
            connector.start(OWNER, ORIGIN)
            await asyncio.gather(connector.session.acquisition, return_exceptions=True)
            self.assertTrue(cloud.closed)
            self.assertEqual(connector.session.phase, {"cancel": Phase.CANCELED, "expire": Phase.EXPIRED,
                "fail": Phase.FAILED, "success": Phase.READY}[outcome])
            pair.assert_not_called()

    async def test_sorted_bounded_homes_and_false_continuation_flags(self):
        cloud = FakeCloud()
        cloud.homes = [{"id": 3}, {"id": 1}, {"id": 3}]
        cloud.records = [{"did": "999"}] * 199
        original = cloud.api
        async def api(path, *args):
            result = await original(path, *args)
            if path in ("/app/home/device_list", "/app/v2/home/home_device_list"):
                result.update(has_more=False, hasmore=0)
            return result
        with patch.object(cloud, "api", side_effect=api) as requests:
            _, public = await self.acquire(cloud)
        self.assertEqual(public["phase"], "ready")
        self.assertEqual([call.args[:2] for call in requests.call_args_list[2:]], [
            ("/app/home/device_list", {"getVirtualModel": True, "getHuamiDevices": 1,
                "get_split_device": False, "support_smart_home": True}),
            ("/app/v2/home/home_device_list", {"home_owner": 123456, "home_id": 1, "limit": 200,
                "get_split_device": True, "support_smart_home": True}),
            ("/app/v2/home/home_device_list", {"home_owner": 123456, "home_id": 3, "limit": 200,
                "get_split_device": True, "support_smart_home": True})])
        for homes in ([{"id": 1}] * 31, {}, [None], [{"id": True}]):
            cloud = FakeCloud()
            cloud.homes = homes
            _, public = await self.acquire(cloud)
            self.assertEqual(public["phase"], "failed")
            self.assertEqual(public["devices"], [])


class QRHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = await create_app(self.root, {ORIGIN})
        self.app.on_startup.clear()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {"Host": "127.0.0.1:8910", "Origin": ORIGIN}
        self.state = self.app[STATE]
        self.cloud = FakeCloud()
        self.state["qr"].cloud_factory = lambda: self.cloud

    async def asyncTearDown(self):
        self.state["device"] = None
        await self.client.close()
        self.temp.cleanup()

    async def post(self, action, body):
        return await self.client.post("/api/xiaomi/qr/" + action, json=body, headers=self.headers)

    async def start_ready(self):
        self.assertEqual((await self.post("start", {"owner": OWNER, "consent": START_CONSENT})).status, 200)
        await self.state["qr"].session.acquisition

    async def test_ak_acquisition_waits_with_private_image_before_auth_or_pair(self):
        connector = self.state["qr"]
        polling = asyncio.get_running_loop().create_future()
        request = self.cloud.request
        poll_url = "https://ak.lp.account.xiaomi.com/lp/s?k=private-ak-key"
        async def response(url, stage, **kwargs):
            if stage == "poll":
                self.assertEqual(url, poll_url)
                self.cloud.calls.append(stage)
                polling.set_result(None)
                await asyncio.Future()
            raw, cookie = await request(url, stage, **kwargs)
            if stage == "create":
                metadata = json.loads(raw)
                metadata["lp"] = poll_url
                raw = json.dumps(metadata).encode()
            return raw, cookie
        with patch.object(self.cloud, "request", side_effect=response), \
                patch.object(self.cloud, "api", new_callable=AsyncMock) as api, \
                patch.object(connector, "pair", new_callable=AsyncMock) as pair, \
                patch("service.device_from_token") as verify:
            self.assertEqual((await self.post("start", {"owner": OWNER, "consent": START_CONSENT})).status, 200)
            session = connector.session
            await asyncio.wait((polling, session.acquisition), timeout=5, return_when=asyncio.FIRST_COMPLETED)
            status = await self.post("status", {"owner": OWNER})
            self.assertEqual(status.status, 200)
            public = await status.json()
            self.assertEqual(public["phase"], "waiting", public)
            self.assertEqual(public["code"], "")
            self.assertTrue(public["image_ready"])
            self.assertTrue(polling.done())
            self.assertFalse(session.acquisition.done())
            self.assertEqual(session.phase, Phase.WAITING)
            self.assertEqual(session.token, "")
            self.assertIsNone(session.binding)
            for secret in ("private-ak-key", "ticket=private", TOKEN, OWNER, SECURITY):
                self.assertNotIn(secret, json.dumps(public) + repr(session))
            image = await self.post("image", {"owner": OWNER})
            self.assertEqual(image.status, 200)
            self.assertEqual(image.headers["Content-Type"], "image/png")
            self.assertEqual(image.headers["Cache-Control"], "no-store")
            self.assertEqual(await image.read(), IMAGE)
            self.assertEqual((await self.post("image", {"owner": "b" * 64})).status, 409)
            self.assertEqual(self.cloud.calls, ["create", "image", "poll"])
            api.assert_not_called()
            pair.assert_not_called()
            verify.assert_not_called()
            self.assertFalse((self.root / "device-token").exists())

    async def test_consent_strict_shapes_duplicate_query_and_origin(self):
        for body in ({"owner": OWNER}, {"owner": OWNER, "consent": "yes"},
                     {"owner": OWNER, "consent": START_CONSENT, "extra": 1},
                     {"owner": "A" * 64, "consent": START_CONSENT}, [], {"owner": True}):
            self.assertEqual((await self.post("start", body)).status, 400)
        for raw in ('{"owner":"' + OWNER + '","owner":"' + OWNER + '","consent":"' + START_CONSENT + '"}', '{"owner":NaN}'):
            response = await self.client.post("/api/xiaomi/qr/start", data=raw,
                    headers={**self.headers, "Content-Type": "application/json"})
            self.assertEqual(response.status, 400)
        response = await self.client.post("/api/xiaomi/qr/start?x=1", json={"owner": OWNER, "consent": START_CONSENT}, headers=self.headers)
        self.assertEqual(response.status, 400)
        response = await self.client.post("/api/xiaomi/qr/start", json={"owner": OWNER, "consent": START_CONSENT},
                                        headers={**self.headers, "Origin": "http://evil.invalid"})
        self.assertEqual(response.status, 403)
        self.assertIsNone(self.state["qr"].session)
        self.assertEqual(self.cloud.calls, [])

    async def test_body_deadline_precedes_any_session_reservation(self):
        from types import SimpleNamespace
        handler = next(route.handler for route in self.app.router.routes()
                       if route.method == "POST" and route.resource.canonical == "/api/xiaomi/qr/{action}")
        async def slow_body():
            await asyncio.sleep(1)
            return b'{}'
        original_timeout = asyncio.timeout
        with patch("service.asyncio.timeout", side_effect=lambda seconds: original_timeout(0.001)):
            with self.assertRaises(QRError) as error:
                await handler(SimpleNamespace(match_info={"action": "start"}, query_string="", read=slow_body))
        self.assertEqual(error.exception.code, "request_timeout")
        self.assertEqual(error.exception.status, 408)
        self.assertIsNone(self.state["qr"].session)

    async def test_status_private_image_no_get_and_bind_gate(self):
        await self.start_ready()
        response = await self.post("status", {"owner": OWNER})
        self.assertEqual(response.status, 200)
        self.assertNotIn(TOKEN, await response.text())
        self.assertNotIn("data:", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((await self.post("status", {"owner": "b" * 64})).status, 409)
        self.assertEqual((await self.post("bind", {"owner": OWNER})).status, 400)
        self.assertFalse((self.root / "device-token").exists())
        self.assertEqual((await self.client.get("/api/xiaomi/qr/status", headers=self.headers)).status, 405)
        self.state["qr"].session.image = IMAGE
        response = await self.post("image", {"owner": OWNER})
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertEqual(await response.read(), IMAGE)

    async def test_queued_cancel_and_expiry_do_not_verify_save_or_protect(self):
        for cancel in (True, False):
            await self.start_ready()
            connector = self.state["qr"]
            session = connector.session
            self.state["decision"]["level"] = self.state["controller"].level = 10
            await self.state["pair_lock"].acquire()
            with patch("service.device_from_token") as verify:
                pending = asyncio.create_task(connector.bind(OWNER, ORIGIN, TARGET["did"]))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertEqual(session.phase, Phase.QUEUED)
                with self.assertRaises(QRError):
                    await connector.bind(OWNER, ORIGIN, "999")
                if cancel:
                    connector.cancel(OWNER, ORIGIN)
                else:
                    session.deadline = time.monotonic() - 1
                    connector.check_expiry(session)
                self.state["pair_lock"].release()
                await pending
                verify.assert_not_called()
            self.assertFalse((self.root / "device-token").exists())
            self.assertEqual(self.state["controller"].level, 10)
            await connector.close()
            self.state["qr"] = QRConnector(connector.pair, lambda: self.cloud)

    async def test_success_uses_existing_verified_persistence_and_budget(self):
        await self.start_ready()
        budget = self.state["motor_schedule"]
        budget.attempts = 1
        fake_device = object()
        with patch("service.device_from_token", return_value=fake_device) as verify:
            response = await self.post("bind", {"owner": OWNER, "consent": BIND_CONSENT, "did": TARGET["did"]})
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["phase"], "bound")
            verify.assert_called_once_with(TOKEN)
            self.assertEqual((self.root / "device-token").read_text(), TOKEN)
            if __import__("os").name == "posix":
                self.assertEqual((self.root / "device-token").stat().st_mode & 0o777, 0o600)
            self.assertIs(self.state["device"], fake_device)
            self.assertIs(self.state["motor_schedule"], budget)
            self.assertEqual(budget.attempts, 1)
            self.assertFalse(self.state["fan"]["confirmed"])
            self.assertEqual(self.state["controller"].level, 16)
            await self.post("bind", {"owner": OWNER, "consent": BIND_CONSENT, "did": TARGET["did"]})
            verify.assert_called_once()


    async def test_bind_requires_exact_fields_and_fixed_string_did(self):
        await self.start_ready()
        valid = {"owner": OWNER, "consent": BIND_CONSENT, "did": TARGET["did"]}
        bodies = [{key: value for key, value in valid.items() if key != "did"},
                  {**valid, "extra": True}, {**valid, "token": TOKEN}]
        bodies += [{**valid, "did": did} for did in (None, True, 70632677, "", "999", [], {})]
        with patch("service.device_from_token") as verify:
            for body in bodies:
                self.assertEqual((await self.post("bind", body)).status, 400)
            verify.assert_not_called()
        self.assertEqual(self.state["qr"].session.phase, Phase.READY)

    async def test_retired_routes_are_404(self):
        for path, method in (("/api/device-token", self.client.post), ("/api/scan", self.client.post),
                             ("/api/xiaomi/bind", self.client.post), ("/api/xiaomi/devices", self.client.get),
                             ("/api/xiaomi/devices", self.client.head),
                             ("/api/ha-certificate.cer", self.client.get),
                             ("/api/ha-certificate.cer", self.client.head)):
            response = await method(path, headers={**self.headers, "Content-Type": "application/json"})
            self.assertEqual(response.status, 404, path)
        response = await self.client.get("/api/status", headers=self.headers)
        self.assertNotIn("official", await response.json())


    async def test_public_http_list_missing_target_does_not_verify_or_save(self):
        self.cloud.records = [{"did": "999", "model": "zhimi.airpurifier.m2",
                               "token": "not-the-target-secret", "isOnline": True}]
        with patch("service.device_from_token") as verify:
            await self.start_ready()
            response = await self.post("status", {"owner": OWNER})
            result = await response.json()
            self.assertEqual(result["phase"], "ready")
            self.assertEqual(result["code"], "target_missing")
            self.assertEqual(result["devices"], [{"did": "999", "name": "未命名设备",
                "model": "zhimi.airpurifier.m2", "ip": None, "online": True,
                "selectable": False, "reason": "unsupported_device"}])
            self.assertNotIn("not-the-target-secret", await response.text())
            rejected = await self.post("bind", {"owner": OWNER, "consent": BIND_CONSENT, "did": TARGET["did"]})
            self.assertEqual(rejected.status, 409)
            verify.assert_not_called()
        self.assertFalse((self.root / "device-token").exists())


if __name__ == "__main__":
    unittest.main()
