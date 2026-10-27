"""Unit tests for proxy.py. They need no docker and no network access.

A fake upstream HTTP server records what it receives, so every assertion is
about bytes that actually crossed the proxy.

Run from the repo root: python3 proxy/test_proxy.py
"""

import http.client
import http.server
import io
import os
import sys
import threading
import time
import importlib.util
import unittest

# Load proxy.py by file path. "import proxy" would pick up the proxy/
# directory as a namespace package when run from the repo root.
_spec = importlib.util.spec_from_file_location(
    "reviewbot_proxy", os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.py")
)
proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proxy)

REAL = "sk-ant-oat01-REAL-unit-test-token-0123456789abcdefghij"
DUMMY = "sk-ant-oat01-dummy"
DUMMY_KEY = "sk-ant-api03-dummy-api-key"


class Upstream(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), UpstreamHandler)
        self.requests = []  # list of (method, path, headers list, body)
        self.release = threading.Event()
        self.released_in_time = None


class UpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _record(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.server.requests.append((self.command, self.path, list(self.headers.items()), body))
        return body

    def do_GET(self):
        self._record()
        data = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self._record()
        if self.path.startswith("/v1/messages/count_tokens"):
            data = b'{"input_tokens":3}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/v1/messages"):
            # Send the first SSE event, then hold the stream open until the
            # test confirms that the client already received that event.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def chunk(b):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b))
                self.wfile.flush()

            chunk(b"event: message_start\ndata: {\"n\":1}\n\n")
            self.server.released_in_time = self.server.release.wait(timeout=5)
            chunk(b"event: content_block_delta\ndata: {\"n\":2}\n\n")
            chunk(b"event: message_stop\ndata: {\"n\":3}\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.up = Upstream()
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.log = io.StringIO()
        upstream_url = "http://127.0.0.1:%d" % self.up.server_address[1]
        self.px = proxy.make_server("127.0.0.1", 0, upstream_url, REAL, log_stream=self.log)
        threading.Thread(target=self.px.serve_forever, daemon=True).start()
        self.port = self.px.server_address[1]

    def tearDown(self):
        self.px.shutdown()
        self.px.server_close()
        self.up.shutdown()
        self.up.server_close()

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def dummy_headers(self):
        return {
            "Authorization": "Bearer " + DUMMY,
            "X-Api-Key": DUMMY_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def assert_no_dummy_upstream(self):
        self.assertTrue(self.up.requests, "upstream received nothing")
        for method, path, headers, body in self.up.requests:
            for name, value in headers:
                self.assertNotIn(DUMMY, value, f"dummy token reached upstream in {name}")
                self.assertNotIn(DUMMY_KEY, value, f"dummy api key reached upstream in {name}")
                self.assertNotEqual(name.lower(), "x-api-key")
            auth = [v for n, v in headers if n.lower() == "authorization"]
            self.assertEqual(auth, ["Bearer " + REAL], "upstream must see exactly one Authorization header, the real one")

    def assert_log_clean(self):
        text = self.log.getvalue()
        for secret in (REAL, DUMMY, DUMMY_KEY, "Bearer", "secret-body-marker"):
            self.assertNotIn(secret, text)

    def test_sse_streams_incrementally_and_token_is_replaced(self):
        c = self.conn()
        c.request("POST", "/v1/messages?beta=true", body=b'{"secret-body-marker":1}', headers=self.dummy_headers())
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")

        # The upstream holds back events 2 and 3 until release is set. If the
        # proxy buffered the body, this read would block until the upstream's
        # 5 second wait expired and released_in_time would be False.
        first = b""
        deadline = time.monotonic() + 3
        while b"\n\n" not in first and time.monotonic() < deadline:
            first += r.read1(65536)
        self.assertIn(b"message_start", first)
        self.assertNotIn(b"content_block_delta", first)
        self.up.release.set()
        rest = r.read()
        self.assertIn(b"content_block_delta", rest)
        self.assertIn(b"message_stop", rest)
        self.assertTrue(self.up.released_in_time, "first SSE event was not delivered before the stream ended")
        c.close()

        self.assertEqual(len(self.up.requests), 1)
        method, path, headers, body = self.up.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/messages?beta=true"))
        self.assertEqual(body, b'{"secret-body-marker":1}')
        self.assert_no_dummy_upstream()
        self.assertIn("POST /v1/messages 200", self.log.getvalue())
        self.assert_log_clean()

    def test_count_tokens_forwarded(self):
        c = self.conn()
        c.request("POST", "/v1/messages/count_tokens", body=b"{}", headers=self.dummy_headers())
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.read(), b'{"input_tokens":3}')
        c.close()
        self.assert_no_dummy_upstream()
        self.assert_log_clean()

    def test_non_allowlisted_paths_are_403_and_never_reach_upstream(self):
        cases = [
            ("GET", "/v1/models"),
            ("GET", "/api/oauth/profile"),
            ("POST", "/v1/complete"),
            ("GET", "/v1/messages"),  # allowlisted path, wrong method
            ("POST", "/v1/messages/../models"),
            ("POST", "/v1/messages/"),
            ("POST", "/v1/Messages"),
            ("POST", "/v1/messages%2Fcount_tokens"),
            ("CONNECT", "api.anthropic.com:443"),
            ("POST", "http://api.anthropic.com/v1/messages"),  # absolute-URL form
        ]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                c = self.conn()
                c.putrequest(method, path, skip_host=True)
                c.putheader("Host", "proxy")
                c.putheader("Authorization", "Bearer " + DUMMY)
                c.endheaders()
                r = c.getresponse()
                r.read()
                self.assertEqual(r.status, 403)
                c.close()
        self.assertEqual(self.up.requests, [], "a denied request reached the upstream")
        self.assertIn("GET /v1/models 403", self.log.getvalue())
        self.assert_log_clean()

    def test_negative_chunk_size_is_400_without_waiting_for_the_client(self):
        # int("-1", 16) is -1, and read(-1) reads until the client closes.
        # The client here keeps the connection open and sends no more. The
        # handler has no read timeout, so a proxy that waited for the body
        # would never answer while the connection stays open; any answer
        # within the generous socket timeout shows it did not wait.
        import socket
        self.assertIsNone(self.px.RequestHandlerClass.timeout)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        try:
            s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: proxy\r\n"
                      b"Transfer-Encoding: chunked\r\n\r\n-1\r\n" + b"A" * 1000)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = s.recv(65536)  # socket.timeout here means no answer
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        self.assertTrue(data.startswith(b"HTTP/1.1 400 "), data[:100])
        self.assertEqual(self.up.requests, [])
        # The server thread writes the log line after sending the response.
        deadline = time.monotonic() + 10
        while "POST /v1/messages 400" not in self.log.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("POST /v1/messages 400", self.log.getvalue())

    def test_detector_fires_on_wrong_token(self):
        # Proves assert_no_dummy_upstream is not a tautology: a proxy that
        # forwards the dummy must fail it.
        self.up.requests.append(("POST", "/v1/messages", [("Authorization", "Bearer " + DUMMY)], b""))
        with self.assertRaises(AssertionError):
            self.assert_no_dummy_upstream()

    def test_refuses_to_start_without_token(self):
        with self.assertRaises(ValueError):
            proxy.make_server("127.0.0.1", 0, "http://127.0.0.1:1", "")

    def test_refuses_allowlist_path_without_leading_slash(self):
        # The allowlist check is the only path check; it relies on this.
        with self.assertRaises(ValueError):
            proxy.make_server("127.0.0.1", 0, "http://127.0.0.1:1", REAL,
                              allowlist={("POST", "http://api.anthropic.com/v1/messages")})


# main() with MAX_LIFETIME_SECONDS patched, run as its own process, which
# must end by itself with status 0.
LIFETIME_RUNNER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("reviewbot_proxy", sys.argv[1])
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
proxy.MAX_LIFETIME_SECONDS = 1
sys.exit(proxy.main())
"""


class LifetimeTest(unittest.TestCase):
    def test_proxy_exits_0_after_its_lifetime(self):
        import subprocess
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy.py")
        env = {"PATH": os.environ.get("PATH", ""), "CLAUDE_CODE_OAUTH_TOKEN": REAL,
               "REVIEWBOT_PROXY_PORT": "0"}
        started = time.monotonic()
        r = subprocess.run([sys.executable, "-I", "-c", LIFETIME_RUNNER, path], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reviewbot-proxy: lifetime of 1s reached", r.stderr)
        self.assertIn("reviewbot-proxy: stopped", r.stderr)
        self.assertNotIn(REAL, r.stderr)
        self.assertLess(time.monotonic() - started, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
