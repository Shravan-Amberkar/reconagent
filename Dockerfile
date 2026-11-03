# Any container host (deployed on Fly.io). Serves the recorded demo run; no API key needed.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
ENV RECON_LLM_CACHE=/app/data/llm_cache
EXPOSE 7860
CMD ["uv", "run", "--no-dev", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "7860"]
