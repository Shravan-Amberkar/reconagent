# Hugging Face Spaces (Docker SDK) / any container host. Serves the cached demo; no key needed.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
ENV RECON_LLM_CACHE=/app/data/llm_cache STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
EXPOSE 7860
CMD ["uv", "run", "--no-dev", "streamlit", "run", "app/streamlit_app.py", \
     "--server.port=7860", "--server.address=0.0.0.0", "--server.headless=true"]
