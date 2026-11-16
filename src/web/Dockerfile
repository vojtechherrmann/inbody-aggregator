FROM python:3.12-slim

WORKDIR /app

# ODBC Driver 18: Azure SQL through the managed identity (same recipe as the monorepo's web).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -sSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*
# Do NOT `apt-get purge curl gnupg && apt-get autoremove` here: autoremove takes libraries the ODBC
# driver needs, and the app then dies at startup with "Can't open lib .../libmsodbcsql-18.*.so" (the
# first deploy, 2026-11-16). Same recipe as the monorepo's src/web/Dockerfile.

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv

# Dependencies first (cache layer), straight from pyproject + the lock's resolution window.
COPY pyproject.toml .
RUN uv pip install --system -r pyproject.toml

# The extraction package (field registry, prompt, wire schema) -- the app loads it from
# /app/extraction (sheetpkg.py). samples/ is never in the build context (.dockerignore).
COPY extraction/validate.py ./extraction/validate.py
COPY extraction/inbody_sheet/ ./extraction/inbody_sheet/

# The app: flat in /app, as tests and local runs import it from src/web.
COPY src/web/*.py ./
COPY src/web/i18n/ ./i18n/
COPY src/web/templates/ ./templates/
COPY src/web/static/ ./static/

# The commit is baked into the image (footer + /healthz), as in the monorepo's weber: a container
# cannot see the tag it was deployed under. The deploy also sets APP_VERSION; both are the same sha.
ARG GIT_SHA=dev
ENV APP_VERSION=$GIT_SHA

# Run as an unprivileged user; one process (the in-memory rate limits assume a single worker).
RUN useradd --system --uid 10001 app
USER app

# No --proxy-headers "*": the client IP is the LAST X-Forwarded-For hop, read by the app itself
# (TRUST_PROXY_HEADERS=1); the first hop is client-controlled.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
