FROM ghcr.io/astral-sh/uv:0.11.6@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754 AS uv
FROM python:3.13.6-slim-trixie@sha256:2a928e11761872b12003515ea59b3c40bb5340e2e5ecc1108e043f92be7e473d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY uv.lock ./
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable \
    && groupadd --gid 10001 trading-research \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin trading-research \
    && mkdir -p /data \
    && chown 10001:10001 /data

USER 10001:10001
ENTRYPOINT ["trading-research"]