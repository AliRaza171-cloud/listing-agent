# One Dockerfile for every service. Build from the REPO ROOT:
#   docker build -f services/Dockerfile --build-arg SERVICE=catalog -t lagent-catalog .
FROM python:3.12-slim

ARG SERVICE
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EVENTS_CONTRACT_PATH=/app/contracts/events.json \
    PORT=8000

WORKDIR /app

# shared infrastructure library
COPY libs/common /libs/common
RUN pip install --no-cache-dir /libs/common

# this service's own dependencies
COPY services/${SERVICE}/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY contracts /app/contracts
COPY services/${SERVICE}/ /app/

# /data/media: catalog's photo volume. Created here so the non-root user owns it
# (a new Docker volume copies this ownership).
RUN useradd --create-home appuser && mkdir -p /data/media && chown appuser /data/media
USER appuser

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
