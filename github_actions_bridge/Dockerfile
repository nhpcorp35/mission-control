FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY verified_case_reader.py .
COPY verified_case_search.py .
COPY verified_case_index.py .
COPY framework_evidence.py .
COPY framework_conflicts.py .
COPY authority_verification.py .
COPY reviewed_authority_store.py .
COPY case_intake.py .
COPY storage_policy.py .
COPY service_auth.py .

CMD ["python", "server.py"]
