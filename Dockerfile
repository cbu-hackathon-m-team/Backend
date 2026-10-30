FROM ghcr.io/astral-sh/uv:0.9.26 AS uv
FROM python:3.12.14-slim-bookworm
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY ossguard ./ossguard
COPY templates ./templates
COPY migrations ./migrations
COPY alembic.ini ./
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
CMD ["uvicorn", "ossguard.api:app", "--host", "0.0.0.0", "--port", "8000"]
