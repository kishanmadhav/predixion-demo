FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.8 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app

ENV DB_PATH=/data/voice_agent.db HOST=0.0.0.0
EXPOSE 8080 9000
CMD ["voice-agent", "serve"]
