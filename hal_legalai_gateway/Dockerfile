FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install as an importable package so registry.json resolves next to modules.
# Copy the package directory (build context is this folder) so production modules
# are included without a brittle per-file manifest. .dockerignore keeps secrets,
# caches, tests, and local state out of the image.
COPY . ./hal_legalai_gateway/

ENV PYTHONPATH=/app
ENV PORT=8080

CMD ["sh", "-c", "uvicorn hal_legalai_gateway.server:app --host 0.0.0.0 --port ${PORT:-8080}"]
