FROM python:3.13-alpine
WORKDIR /app
COPY app.py /app/app.py
# Docker's Unix socket is root-owned on the host. This service is therefore
# privileged by design, but its HTTP API is an allowlist and has no shell/file
# execution endpoint; Hermes never receives this access.
CMD ["python", "/app/app.py"]
