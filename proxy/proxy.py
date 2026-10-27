#!/usr/bin/env python3
"""reviewbot proxy.

A reverse proxy that accepts plain HTTP from the review container and
forwards allowlisted requests to the Anthropic API over HTTPS. It removes the
incoming Authorization and X-Api-Key headers and sets
"Authorization: Bearer <real token>" from the CLAUDE_CODE_OAUTH_TOKEN
environment variable. Response bodies are streamed to the client as they
arrive, so server-sent events are not buffered.

Logging is limited to method, path (without query string), status and
duration. Header values and bodies are never logged.

Environment:
  CLAUDE_CODE_OAUTH_TOKEN   required, the real OAuth token
  REVIEWBOT_UPSTREAM        optional, default https://api.anthropic.com
  REVIEWBOT_PROXY_PORT      optional, default 8080
"""

import http.client
import http.server
import os
import signal
import ssl
import sys
import threading
import time
import urllib.parse

# (method, path) pairs that are forwarded. The path is compared exactly, after
# removing the query string. Everything else gets 403.
ALLOWLIST = frozenset(
    {
        ("POST", "/v1/messages"),
        ("POST", "/v1/messages/count_tokens"),
    }
)

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Request headers that are never forwarded upstream.
STRIP_REQUEST = HOP_BY_HOP | {"host", "content-length", "authorization", "x-api-key", "cookie", "expect"}
# Response headers that are not copied back to the client. Framing is redone
# by this proxy.
STRIP_RESPONSE = HOP_BY_HOP | {"content-length", "set-cookie"}

MAX_REQUEST_BODY = 64 * 1024 * 1024
# Seconds after which the proxy stops by itself, so a proxy whose orchestrator
# died never outlives this. Must exceed readiness wait + review timeout +
# cleanup (15 + 1200 + 50), so a request alive at expiry belongs to no live review.
MAX_LIFETIME_SECONDS = 1800
UPSTREAM_TIMEOUT = 900
READ_SIZE = 65536


def _safe_path(raw):
    """Return the path without its query string, made printable and bounded."""
    path = raw.split("?", 1)[0]
    cleaned = "".join(c if 0x21 <= ord(c) < 0x7F else "?" for c in path)
    if len(cleaned) > 200:
        cleaned = cleaned[:200] + "..."
    return cleaned


class _BodyError(Exception):
    pass


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "reviewbot-proxy"
    sys_version = ""

    # Set by make_server on a subclass.
    upstream = None  # urllib.parse.SplitResult
    token = None
    log_stream = sys.stderr
    allowlist = ALLOWLIST

    # Silence the default logger, which would print the full request line
    # (including the query string) and error details.
    def log_message(self, format, *args):  # noqa: A002
        pass

    def _log(self, status, started, note=""):
        method = getattr(self, "command", None) or "-"
        path = _safe_path(getattr(self, "path", None) or "-")
        ms = int((time.monotonic() - started) * 1000)
        line = f"{method} {path} {status} {ms}ms"
        if note:
            line += f" {note}"
        try:
            self.log_stream.write(line + "\n")
            self.log_stream.flush()
        except Exception:
            pass

    def send_error(self, code, message=None, explain=None):
        # Called by the base class for malformed requests. Keep the response
        # minimal and log only the status.
        self.close_connection = True
        try:
            self.send_response_only(code)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            pass
        self._log(code, time.monotonic(), "malformed")

    def _reply_simple(self, status, started, note=""):
        body = b""
        self.send_response_only(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self._log(status, started, note)

    def _read_request_body(self):
        te = self.headers.get("Transfer-Encoding", "")
        if te:
            if te.strip().lower() != "chunked":
                raise _BodyError("unsupported transfer-encoding")
            chunks = []
            total = 0
            while True:
                line = self.rfile.readline(1024)
                if not line:
                    raise _BodyError("truncated chunked body")
                size = int(line.split(b";", 1)[0].strip(), 16)
                if size < 0:
                    # read(-1) would read until the client closes, unbounded
                    raise _BodyError("negative chunk size")
                if size == 0:
                    # Discard trailers.
                    while True:
                        trailer = self.rfile.readline(8192)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > MAX_REQUEST_BODY:
                    raise _BodyError("body too large")
                data = self.rfile.read(size)
                if len(data) != size:
                    raise _BodyError("truncated chunked body")
                chunks.append(data)
                self.rfile.readline(8)
            return b"".join(chunks)
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            n = int(length)
        except ValueError:
            raise _BodyError("bad content-length")
        if n < 0 or n > MAX_REQUEST_BODY:
            raise _BodyError("bad content-length")
        data = self.rfile.read(n)
        if len(data) != n:
            raise _BodyError("truncated body")
        return data

    def _upstream_headers(self):
        connection_tokens = set()
        for value in self.headers.get_all("Connection", []):
            connection_tokens.update(t.strip().lower() for t in value.split(","))
        out = []
        for name, value in self.headers.items():
            lname = name.lower()
            if lname in STRIP_REQUEST or lname in connection_tokens:
                continue
            out.append((name, value))
        out.append(("Host", self.upstream.netloc))
        out.append(("Authorization", "Bearer " + self.token))
        return out

    def _open_upstream(self):
        host = self.upstream.hostname
        port = self.upstream.port
        if self.upstream.scheme == "https":
            ctx = ssl.create_default_context()
            return http.client.HTTPSConnection(host, port or 443, timeout=UPSTREAM_TIMEOUT, context=ctx)
        return http.client.HTTPConnection(host, port or 80, timeout=UPSTREAM_TIMEOUT)

    def _handle(self):
        started = time.monotonic()
        path_only = self.path.split("?", 1)[0]
        # The allowlist is the only check on method and path. Every allowlisted
        # path starts with "/" (make_server refuses any other), so an
        # absolute-URL request ("POST http://host/v1/messages") or a CONNECT
        # target never matches and gets 403 here.
        if (self.command, path_only) not in self.allowlist:
            # Do not read or forward the body. Close the connection so an
            # unread body cannot be misparsed as the next request.
            self._reply_simple(403, started, "denied")
            return
        try:
            body = self._read_request_body()
        except (_BodyError, ValueError):
            self._reply_simple(400, started, "bad-body")
            return

        conn = self._open_upstream()
        try:
            try:
                conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for name, value in self._upstream_headers():
                    conn.putheader(name, value)
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                self._reply_simple(502, started, "upstream-error:" + type(exc).__name__)
                return

            client_http11 = self.request_version == "HTTP/1.1"
            length = resp.getheader("Content-Length")
            upstream_te = resp.getheader("Transfer-Encoding")
            no_body = self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200
            use_chunked = False

            self.send_response_only(resp.status, resp.reason)
            for name, value in resp.getheaders():
                if name.lower() in STRIP_RESPONSE:
                    continue
                self.send_header(name, value)
            if no_body:
                self.send_header("Content-Length", "0" if self.command != "HEAD" else (length or "0"))
            elif length is not None and not upstream_te:
                self.send_header("Content-Length", length)
            elif client_http11:
                use_chunked = True
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.close_connection = True
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()

            if not no_body:
                try:
                    while True:
                        chunk = resp.read1(READ_SIZE)
                        if not chunk:
                            break
                        if use_chunked:
                            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        else:
                            self.wfile.write(chunk)
                        self.wfile.flush()
                    if use_chunked:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                    self._log(resp.status, started, "client-gone")
                    return
                except (OSError, http.client.HTTPException) as exc:
                    # Upstream broke mid-body. The status line is already
                    # sent, so the only signal left is closing the stream.
                    self.close_connection = True
                    self._log(resp.status, started, "upstream-broke:" + type(exc).__name__)
                    return
            self._log(resp.status, started)
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_CONNECT = _handle


class ProxyServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(host, port, upstream, token, log_stream=None, allowlist=ALLOWLIST):
    if not token:
        raise ValueError("token is empty")
    parsed = urllib.parse.urlsplit(upstream)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/"):
        raise ValueError("upstream must be scheme://host[:port]")
    if not all(path.startswith("/") for _, path in allowlist):
        raise ValueError("every allowlisted path must start with /")
    handler = type(
        "BoundProxyHandler",
        (ProxyHandler,),
        {
            "upstream": parsed,
            "token": token,
            "log_stream": log_stream or sys.stderr,
            "allowlist": frozenset(allowlist),
        },
    )
    return ProxyServer((host, port), handler)


def main():
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not token.strip():
        sys.stderr.write("reviewbot-proxy: CLAUDE_CODE_OAUTH_TOKEN is not set\n")
        return 2
    upstream = os.environ.get("REVIEWBOT_UPSTREAM", "https://api.anthropic.com")
    port = int(os.environ.get("REVIEWBOT_PROXY_PORT", "8080"))
    server = make_server("0.0.0.0", port, upstream, token.strip())
    sys.stderr.write(f"reviewbot-proxy: listening on :{port}, upstream {upstream}\n")
    sys.stderr.flush()

    # As PID 1 in a container, SIGTERM has no default action, so "docker
    # stop" would wait and then SIGKILL. shutdown() blocks until
    # serve_forever returns, so it must run on another thread.
    def _stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # A response still in flight at this point belongs to no live review
    # (see MAX_LIFETIME_SECONDS), so it is cut off when the process exits.
    def _expire():
        sys.stderr.write(f"reviewbot-proxy: lifetime of {MAX_LIFETIME_SECONDS}s reached\n")
        sys.stderr.flush()
        server.shutdown()

    timer = threading.Timer(MAX_LIFETIME_SECONDS, _expire)
    timer.daemon = True
    timer.start()
    server.serve_forever()
    timer.cancel()
    server.server_close()
    sys.stderr.write("reviewbot-proxy: stopped\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
