# Image backend FastAPI (Python 3.12)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Installer les dépendances à partir de pyproject.toml
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# Copier le code de l'application et la configuration des migrations
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

EXPOSE 8000

# Appliquer les migrations Alembic puis lancer le serveur ASGI (voir entrypoint.sh)
CMD ["./entrypoint.sh"]
