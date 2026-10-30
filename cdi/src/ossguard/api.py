"""Local integration API for the execution backend (single process/worker)."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from ossguard.contracts import Snapshot
from ossguard.engine import Engine
from ossguard.node_red import NodeRedAdapter
from ossguard.storage import BusyError, Repository


def create_app(repository=None, adapters=None):
    tasks = set()

    @asynccontextmanager
    async def lifespan(app):
        repo = repository or Repository(os.environ["DATABASE_URL"])
        root = Path(os.environ.get("OSSGUARD_ROOT", Path.cwd()))
        engine = Engine(repo, adapters if adapters is not None else {"node-red": NodeRedAdapter(root)})
        app.state.repository, app.state.engine = repo, engine
        await repo.own_worker()
        try:
            await engine.recover()
            yield
        finally:
            for task in list(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await repo.close()

    app = FastAPI(title="OSSGuard execution backend", lifespan=lifespan)

    def done(task):
        tasks.discard(task)
        if not task.cancelled() and task.exception():
            logging.getLogger(__name__).error("Validation worker failed: %s", type(task.exception()).__name__)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        async with app.state.repository.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.post("/api/validations", status_code=202)
    async def start(snapshot: Snapshot):
        try:
            run_id = await app.state.engine.submit(snapshot)
        except BusyError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        task = asyncio.create_task(app.state.engine.run(run_id))
        tasks.add(task)
        task.add_done_callback(done)
        return {"id": run_id, "status": "QUEUED"}

    @app.get("/api/validations/{run_id}")
    async def result(run_id: UUID):
        stored = await app.state.repository.get(str(run_id))
        if stored is None:
            raise HTTPException(404, "Validation not found")
        return stored

    return app


app = create_app()
