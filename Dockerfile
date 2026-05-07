# Multi-stage build for the corp-llm-gateway runtime image.
# Stage 1: build the wheel; Stage 2: slim runtime.

FROM python:3.12-slim AS build
WORKDIR /build
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN python -m build --wheel

FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="corp-llm-gateway" \
      org.opencontainers.image.source="https://git.corp.lan/.../corp-llm-gateway"

# Minimal runtime — gateway is loaded by LiteLLM as a Python callback.
RUN useradd --create-home --shell /bin/bash app
WORKDIR /app

COPY --from=build /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

USER app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The image exposes the CLIs; LiteLLM proxy mounts this as a callback.
ENTRYPOINT ["/usr/local/bin/gateway-admin"]
CMD ["--help"]
