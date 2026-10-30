"""Generate the team's trusted demo artifacts. Never consumes AI-generated code."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "fixtures" / "node-red"
DEST.mkdir(parents=True, exist_ok=True)


def flow(broken=False):
    nodes = [{"id": "orders", "type": "tab", "label": "OSSGuard synthetic order demo"}]

    def node(node_id, kind, wires, **kwargs):
        nodes.append(
            {
                "id": node_id,
                "type": kind,
                "z": "orders",
                "name": node_id,
                "x": 240 + len(nodes) * 30,
                "y": 100 + len(nodes) * 35,
                "wires": wires,
                **kwargs,
            }
        )

    node("http_in", "http in", [["receive_setup"]], url="/orders", method="post", upload=False, swaggerDoc="")
    node(
        "receive_setup",
        "function",
        [["order_receive"]],
        outputs=1,
        func="""
msg.order = msg.payload;
if (!Number.isInteger(msg.order.quantity) || msg.order.quantity <= 0) {
    msg.statusCode = 422; node.error('invalid quantity', msg); return null;
}
msg.correlation = {
    'X-Run-Id': msg.req.headers['x-run-id'],
    'X-Env-Id': msg.req.headers['x-env-id'],
    'X-Case-Id': msg.req.headers['x-case-id']
};
msg.headers = {...msg.correlation, 'X-Node-Id':'order_receive', 'Content-Type':'application/json'};
msg.payload = {}; return msg;
""",
    )

    def http(node_id, endpoint, next_node):
        node(
            node_id,
            "http request",
            [[next_node]],
            method="POST",
            ret="obj",
            paytoqs="ignore",
            url=f"http://mock:8080/{endpoint}",
            persist=False,
            authType="",
            senderr=False,
            headers=[],
            tls="",
            proxy="",
            insecureHTTPParser=False,
        )

    def setup(node_id, request_id, next_node):
        node(
            node_id,
            "function",
            [[next_node]],
            outputs=1,
            func=f"msg.headers = {{...msg.correlation, 'X-Node-Id':'{request_id}', 'Content-Type':'application/json'}}; msg.payload = {{quantity:msg.order.quantity}}; return msg;",
        )

    http("order_receive", "received", "payment_setup")
    setup("payment_setup", "payment_check", "payment_check")
    http("payment_check", "payment", "payment_route")
    node(
        "payment_route",
        "function",
        [["inventory_setup"], ["finish"]],
        outputs=2,
        func="return msg.payload.paid ? [msg, null] : [null, msg];",
    )
    setup("inventory_setup", "inventory_decrement", "inventory_decrement")
    http("inventory_decrement", "inventory", "inventory_route")
    node(
        "inventory_route",
        "function",
        [["finish" if broken else "shipping_setup"], ["finish"]],
        outputs=2,
        func="return msg.payload.applied ? [msg, null] : [null, msg];",
    )
    setup("shipping_setup", "shipping_request", "shipping_request")
    http("shipping_request", "shipping", "finish")
    node(
        "finish",
        "function",
        [["http_out"]],
        outputs=1,
        func="msg.headers = {'Content-Type':'application/json'}; msg.statusCode=200; msg.payload={accepted:true, stock:msg.payload.stock}; return msg;",
    )
    node("http_out", "http response", [], statusCode="", headers={})
    node("health_in", "http in", [["health_body"]], url="/health", method="get", upload=False, swaggerDoc="")
    node("health_body", "function", [["health_out"]], outputs=1, func="msg.payload={ready:true}; return msg;")
    node("health_out", "http response", [], statusCode="200", headers={})
    return nodes


for name, broken in [("normal", False), ("missing_shipping", True), ("fixed", False)]:
    (DEST / f"{name}.json").write_text(json.dumps(flow(broken), indent=2), encoding="utf-8")

cases = []
for case_id, paid, stock, quantity, remaining, shipping in [
    ("T01", True, 10, 2, 8, 1),
    ("T02", True, 2, 2, 0, 1),
    ("T03", False, 10, 2, 10, 0),
    ("T04", True, 1, 2, 1, 0),
]:
    expectations = [
        {"op": "eq", "path": "status_code", "expected": 200},
        {"op": "eq", "path": "mock.stock", "expected": remaining},
        {
            "op": "event_count",
            "step_key": "shipping",
            "event_type": "shipping_requested",
            "expected": shipping,
        },
        {
            "op": "event_count",
            "step_key": "inventory",
            "event_type": "inventory_decremented",
            "expected": shipping,
        },
    ]
    if shipping:
        expectations.append(
            {"op": "event_order", "expected": ["received", "payment", "inventory", "shipping"]}
        )
    cases.append(
        {
            "id": case_id,
            "input": {"paid": paid, "stock": stock, "quantity": quantity},
            "expectations": expectations,
        }
    )

snapshot = {
    "target_type": "node-red",
    "confirmed": True,
    "suite_revision": "demo-v1",
    "rules": "Paid orders with sufficient stock decrement inventory and request shipping once; otherwise neither. Synthetic team-confirmed fixture.",
    "baseline": {"image": "nodered/node-red:4.0.9", "artifact": "normal"},
    "candidate": {"image": "nodered/node-red:4.1.7", "artifact": "missing_shipping"},
    "cases": cases,
    "step_map": {
        "order_receive": "received",
        "payment_check": "payment",
        "inventory_decrement": "inventory",
        "shipping_request": "shipping",
    },
}
(DEST / "suite.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
