FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV  UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./

RUN uv sync --no-dev --frozen --no-install-project 
COPY src ./src

RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm AS runtime
RUN useradd --create-home --uid 1000 sre
WORKDIR /app

COPY --from=builder --chown=sre:sre /app /app
ENV PATH="/app/.venv/bin:$PATH"

USER sre
EXPOSE 7070

ENTRYPOINT ["tsa"]
CMD ["api"]

