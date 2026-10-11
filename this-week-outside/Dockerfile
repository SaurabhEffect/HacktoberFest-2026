FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY fieldguide ./fieldguide
COPY web ./web

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# --timeout must exceed LLM_TIMEOUT (default 120s) so slow CPU-only models can finish.
CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "2", "--timeout", "180", "app:app"]
