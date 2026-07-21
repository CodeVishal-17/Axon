FROM python:3.12-slim

# Never buffer logs (we want them live in `docker compose logs`) and never
# write .pyc files into the bind-mounted source tree.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so source edits don't bust the pip layer cache.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY . .

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

# Production-style default; docker-compose overrides with --reload for dev.
# The worker (T1.3) will reuse this image with a different command.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "axon.main:app", "--host", "0.0.0.0", "--port", "8000"]
