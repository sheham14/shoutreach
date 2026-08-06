FROM python:3.11-slim

WORKDIR /app

# Install from the pinned list rather than naming packages inline -- the old
# Dockerfile installed only flask and gunicorn, so anything added to
# requirements.txt silently never made it into the image.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py db.py sender.py scheduler.py email_validator.py ./
COPY templates/ templates/
# static/ was previously not copied, so the container served an app with no
# CSS and no JavaScript.
COPY static/ static/

ENV HOST=0.0.0.0
ENV PORT=8080

EXPOSE 8080

# The scraper is intentionally absent from this image. It drives a real Chrome
# window for CAPTCHA solving, which a container has no display for -- it runs
# on the operator's machine via scraper_worker.py and talks to this app over
# the API. See templates/sections/scraper.html.

# Single worker — required so only one scheduler thread runs
CMD ["gunicorn", "app:app", "--workers", "1", "--bind", "0.0.0.0:8080", "--timeout", "120"]
