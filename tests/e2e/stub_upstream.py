#!/usr/bin/env python3
"""A stand-in for the Anthropic API that answers every request with one error.

Usage: stub_upstream.py PORT STATUS

Every request is answered with STATUS and the JSON error body the Anthropic
API sends for that status (https://docs.claude.com/en/api/errors): 400
invalid_request_error, 401 authentication_error, 403 permission_error, 404
not_found_error, 429 rate_limit_error (with retry-after: 1), 500 api_error, 529
overloaded_error. Each request is logged to stderr as "METHOD PATH STATUS"
so the number of attempts the client made can be counted. Standard library
only; runs in the e2e proxy image (reviewbot-e2e-proxy).
"""

import http.server
import json
import sys

ERRORS = {
    400: ("invalid_request_error", "stub: invalid request"),
    401: ("authentication_error", "stub: invalid bearer token"),
    403: ("permission_error", "stub: permission denied"),
    404: ("not_found_error", "stub: model: not found"),
    429: ("rate_limit_error", "stub: rate limited"),
    500: ("api_error", "stub: internal server error"),
    529: ("overloaded_error", "stub: overloaded"),
}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    status = 500

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        kind, message = ERRORS[self.status]
        body = json.dumps({"type": "error", "error": {"type": kind, "message": message},
                           "request_id": "req_stub"}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("request-id", "req_stub")
        if self.status == 429:
            self.send_header("retry-after", "1")
        self.end_headers()
        self.wfile.write(body)
        sys.stderr.write(f"{self.command} {self.path.split('?')[0]} {self.status}\n")
        sys.stderr.flush()

    do_GET = do_POST = do_HEAD = _answer

    def log_message(self, format, *args):  # noqa: A002
        pass


def main(argv):
    port, status = int(argv[1]), int(argv[2])
    if status not in ERRORS:
        sys.exit(f"stub_upstream.py: no error body for status {status}")
    Handler.status = status
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write(f"stub upstream on :{port} answering {status}\n")
    sys.stderr.flush()
    server.serve_forever()


if __name__ == "__main__":
    main(sys.argv)
