FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY storage_server.py .
COPY storage_policy.py .

CMD ["python", "server.py"]
