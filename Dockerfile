# Multi-stage build: compile dependencies in a full image, run in a slim one.
# This app is a CLI pipeline (LangGraph graph runner) + an MCP server, not a
# web service -- there's no HTTP API to expose, so no EXPOSE/uvicorn here.
# Connects out to Postgres (Neon) and an LLM provider via env vars; nothing
# runs locally except the Python process itself.

FROM python:3.11-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY sql/ ./sql/

# --frozen: fail the build if uv.lock doesn't match pyproject.toml, instead
# of silently re-resolving and drifting from what's actually committed.
RUN uv sync --no-dev --frozen

FROM python:3.11-slim

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src ./src
COPY --from=builder /app/sql ./sql
COPY pyproject.toml ./

# data/output and data/cache are written at runtime (dataset generation,
# investigation cache) -- COPY defaults to root ownership, which would
# make those writes fail once running as appuser below.
RUN mkdir -p /app/data && chown -R appuser:appuser /app

ENV PATH="/app/.venv/bin:$PATH"
USER appuser

# No default CMD -- this image runs different modules depending on the task
# (generate data, load DB, run the graph, score a run). Specify at `docker run`:
#   docker run --env-file .env recon-engine python -m recon_engine.graph.run_classification
#   docker run --env-file .env recon-engine python -m recon_engine.data_gen.generate_dataset
#   docker run --env-file .env recon-engine python -m recon_engine.evaluation.evaluate_agent_resolutions
ENTRYPOINT []
