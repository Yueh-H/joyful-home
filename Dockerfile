FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY server ./server
RUN pip install --no-cache-dir uv==0.7.19 && uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
COPY web ./web
RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app:app /app
USER app
EXPOSE 8765
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8765"]
