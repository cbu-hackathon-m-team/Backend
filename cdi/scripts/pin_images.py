"""Resolve the two approved public image tags; never executes containers."""

import json
from pathlib import Path

import httpx

from ossguard.node_red import ALLOWED_IMAGES

with httpx.Client(timeout=30) as client:
    auth = client.get(
        "https://auth.docker.io/token",
        params={"service": "registry.docker.io", "scope": "repository:nodered/node-red:pull"},
    )
    auth.raise_for_status()
    token = auth.json()["token"]
    locked = {}
    for image in sorted(ALLOWED_IMAGES):
        tag = image.rsplit(":", 1)[1]
        response = client.get(
            f"https://registry-1.docker.io/v2/nodered/node-red/manifests/{tag}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
            },
        )
        response.raise_for_status()
        digest = response.headers["Docker-Content-Digest"]
        locked[image] = f"nodered/node-red@{digest}"
    path = Path(__file__).resolve().parents[1] / "fixtures/node-red/images.lock.json"
    path.write_text(json.dumps(locked, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(locked, indent=2))
