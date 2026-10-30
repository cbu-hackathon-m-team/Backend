"""Trusted loopback gateway into an internal test network; fixed routes only."""

import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROUTES = {
    ("GET", "/mock/health"): ("mock", 8080, "/health"),
    ("GET", "/mock/state"): ("mock", 8080, "/state"),
    ("POST", "/mock/reset"): ("mock", 8080, "/reset"),
    ("GET", "/target/health"): ("target", 1880, "/health"),
    ("POST", "/target/orders"): ("target", 1880, "/orders"),
}


def make_server(host="0.0.0.0", port=8080):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, content_type="application/json", transport_error=False):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if transport_error:
                self.send_header("X-OSSGuard-Transport-Error", "1")
            self.end_headers()
            self.wfile.write(body)

        def forward(self):
            route = ROUTES.get((self.command, self.path))
            if route is None:
                return self.reply(404, b'{"error":"route_not_allowed"}')
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 65536 or self.headers.get("Transfer-Encoding"):
                    return self.reply(400, b'{"error":"invalid_body_size"}')
            except ValueError:
                return self.reply(400, b'{"error":"invalid_body_size"}')
            connection = http.client.HTTPConnection(route[0], route[1], timeout=120)
            try:
                headers = {
                    key: self.headers[key]
                    for key in ("Content-Type", "X-Run-Id", "X-Env-Id", "X-Case-Id")
                    if key in self.headers
                }
                connection.request(self.command, route[2], body=self.rfile.read(length), headers=headers)
                response = connection.getresponse()
                body = response.read(1_048_577)
                if len(body) > 1_048_576:
                    raise ValueError("response_too_large")
                self.reply(response.status, body, response.getheader("Content-Type", "application/json"))
            except (OSError, ValueError, http.client.HTTPException) as error:
                self.reply(502, json.dumps({"error": type(error).__name__}).encode(), transport_error=True)
            finally:
                connection.close()

        do_GET = forward
        do_POST = forward

    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    make_server().serve_forever()
