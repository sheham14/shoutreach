FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir flask>=3.0 gunicorn>=21.0

COPY app.py db.py sender.py scheduler.py ./
COPY templates/ templates/

ENV HOST=0.0.0.0
ENV PORT=8080

EXPOSE 8080

# Single worker — required so only one scheduler thread runs
CMD ["gunicorn", "app:app", "--workers", "1", "--bind", "0.0.0.0:8080", "--timeout", "120"]
