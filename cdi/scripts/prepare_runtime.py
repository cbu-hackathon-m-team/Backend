"""Run after installing/starting Docker. Pull pinned images and build trusted mock."""

import json
from pathlib import Path

import docker

root = Path(__file__).resolve().parents[1]
client = docker.from_env(timeout=300)
try:
    client.ping()
    for reference in json.loads((root / "fixtures/node-red/images.lock.json").read_text()).values():
        image = client.images.pull(reference)
        print(f"Prepared {reference}: {image.id}")
    image, _ = client.images.build(
        path=str(root), dockerfile="infra/mock.Dockerfile", tag="ossguard-mock:1", rm=True
    )
    print(f"Prepared mock: {image.id}")
finally:
    client.close()
