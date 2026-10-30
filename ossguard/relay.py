"""Platform-controlled HTTP relay; target containers stay on an internal Docker network."""

import http.client
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def forward(self):
        prefix, separator, path = self.path.lstrip("/").partition("/")
        routes = {
            "app": (os.environ["APP_HOST"], int(os.environ["APP_PORT"])),
            "mock": (os.environ["MOCK_HOST"], 8080),
        }
        if not separator or prefix not in routes:
            return self.send_error(404)
        size = int(self.headers.get("Content-Length", 0))
        if not 0 <= size <= 262144:
            return self.send_error(413)
        connection = http.client.HTTPConnection(*routes[prefix], timeout=15)
        try:
            body = self.rfile.read(size) if size else None
            connection.request(
                self.command, "/" + path, body=body, headers={"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Connection", "close")
            self.end_headers()
            while chunk := response.read(8192):
                self.wfile.write(chunk)
        except (OSError, http.client.HTTPException):
            self.send_error(502)
        finally:
            connection.close()
            self.close_connection = True

    do_GET = forward
    do_POST = forward
    do_PUT = forward
    do_PATCH = forward
    do_DELETE = forward

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
