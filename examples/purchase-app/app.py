"""Deliberate compatibility example: model/dict equality changed in Pydantic v2."""

import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from pydantic import BaseModel


class Decision(BaseModel):
    approved: bool


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.respond(200 if self.path == "/health" else 404, {"ready": True})

    def do_POST(self):
        if self.path != "/orders":
            return self.respond(404, {})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        stock, quantity = body["stock"], body["quantity"]
        approved = body["paid"] and stock >= quantity
        stock_after = stock - quantity if approved else stock
        decision = Decision(approved=approved)
        # This is the intentional compatibility issue; not a Pydantic defect.
        if decision == {"approved": True}:
            request = urllib.request.Request(
                os.environ["SHIPPING_BASE_URL"] + "/shipping",
                data=json.dumps({"order_id": body["order_id"]}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
        self.respond(200, {"order_id": body["order_id"], "stock_after": stock_after})


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
