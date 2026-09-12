FROM ghcr.io/astral-sh/uv:0.9 AS uv
FROM python:3.13-slim
COPY --from=uv /uv /uvx /usr/local/bin/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 MOBILE_DATA_DIR=/data
USER app
EXPOSE 8081
CMD ["python", "-m", "korail_bot.mobile", "serve", "--host", "0.0.0.0", "--port", "8081"]
