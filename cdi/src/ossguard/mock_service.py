"""Isolated demo-only mock. Each environment gets its own process/state."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.value = None


def make_server(host="0.0.0.0", port=8080):
    state = State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            with state.lock:
                if self.path == "/health":
                    return self.send(200, {"ready": True})
                if self.path == "/state":
                    return self.send(200, state.value or {})
                self.send(404, {"error": "not_found"})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    return self.send(400, {"error": "invalid_body_size"})
                data = json.loads(self.rfile.read(length))
                with state.lock:
                    if self.path == "/reset":
                        if (
                            type(data.get("stock")) is not int
                            or data["stock"] < 0
                            or type(data.get("paid")) is not bool
                            or not all(
                                isinstance(data.get(k), str) and data[k]
                                for k in ("run_id", "env_id", "case_id")
                            )
                        ):
                            return self.send(422, {"error": "invalid_reset"})
                        state.value = {
                            key: data[key] for key in ("run_id", "env_id", "case_id", "stock", "paid")
                        }
                        state.value.update(events=[], shipping_count=0)
                        return self.send(200, state.value)
                    current = state.value
                    if current is None or any(
                        self.headers.get(f"X-{key.replace('_', '-')}") != current[key]
                        for key in ("run_id", "env_id", "case_id")
                    ):
                        return self.send(409, {"error": "correlation_mismatch"})
                    event_type, payload = "", {}
                    if self.path == "/received":
                        event_type = "order_received"
                    elif self.path == "/payment":
                        event_type, payload = "payment_checked", {"paid": current["paid"]}
                    elif self.path == "/inventory":
                        quantity = data.get("quantity")
                        if type(quantity) is not int or quantity <= 0:
                            return self.send(422, {"error": "invalid_quantity"})
                        applied = current["paid"] and current["stock"] >= quantity
                        if applied:
                            current["stock"] -= quantity
                        event_type = "inventory_decremented" if applied else "inventory_rejected"
                        payload = {"stock": current["stock"], "applied": applied}
                    elif self.path == "/shipping":
                        current["shipping_count"] += 1
                        event_type, payload = "shipping_requested", {"count": current["shipping_count"]}
                    else:
                        return self.send(404, {"error": "not_found"})
                    current["events"].append(
                        {
                            "node_id": self.headers.get("X-Node-Id", "unknown"),
                            "event_type": event_type,
                            "payload": payload,
                            "sequence": len(current["events"]) + 1,
                        }
                    )
                    self.send(200, {"stock": current["stock"], "paid": current["paid"], **payload})
            except (ValueError, TypeError, KeyError):
                self.send(422, {"error": "invalid_input"})

    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    make_server().serve_forever()
