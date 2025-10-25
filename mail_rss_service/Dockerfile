FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev

COPY src/* ./src
COPY *.py .

ENV PORT=8000
EXPOSE 8000

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]


# docker run -d \
#   --name mail-rss \
#   -p 8000:8000 \
#   --env-file .env \
#   mail-rss-service