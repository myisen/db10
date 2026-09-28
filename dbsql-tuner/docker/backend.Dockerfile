FROM python:3.11-slim

WORKDIR /app

# Install system deps for oracledb Thick mode (we ship Instant Client separately in docker-compose).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libaio1 libnsl2 libtns2 libz-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/.env.example ./.env.example
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini

EXPOSE 8000
CMD ["python", "-m", "app"]
