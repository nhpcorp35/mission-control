FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install as an importable package so registry.json resolves next to modules.
COPY __init__.py config.py health.py registry.py request_context.py server.py \
     auth.py forwarding.py mcp_server.py ./hal_legalai_gateway/
COPY registry.json ./hal_legalai_gateway/

ENV PYTHONPATH=/app
ENV PORT=8080

CMD ["sh", "-c", "uvicorn hal_legalai_gateway.server:app --host 0.0.0.0 --port ${PORT:-8080}"]
