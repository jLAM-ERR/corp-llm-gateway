FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e ".[dev]"
ENV PYTHONPATH=/app/src
CMD ["pytest", "-q", "tests/e2e"]
