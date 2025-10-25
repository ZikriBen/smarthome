FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

COPY . .

ENV PORT=8000
EXPOSE 8000

CMD ["uv", "run", "fastapi", "run", "main:app", "--host", "0.0.0.0", "--port", "8000"]
