FROM python:3.13.14-slim-trixie AS sqlite-build
RUN apt-get update && apt-get install -y --no-install-recommends gcc make ca-certificates curl && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends libc6-dev && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://sqlite.org/2026/sqlite-autoconf-3510300.tar.gz -o sqlite.tar.gz \
 && echo '81f5be397049b0cae1b167f2225af7646fc0f82e4a9b3c48c9ea3a533e21d77a  sqlite.tar.gz' | sha256sum -c - \
 && tar xzf sqlite.tar.gz \
 && cd sqlite-autoconf-3510300 \
 && ./configure --prefix=/opt/sqlite --disable-static --disable-readline \
 && make -j2 && make install

FROM python:3.13.14-slim-trixie AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LD_LIBRARY_PATH=/opt/sqlite/lib PYTHONPATH=/app/backend
COPY --from=sqlite-build /opt/sqlite/lib/libsqlite3.so* /opt/sqlite/lib/
RUN python -c "import sqlite3; assert sqlite3.sqlite_version_info >= (3,51,3), sqlite3.sqlite_version"
WORKDIR /app

FROM node:24.13.0-slim AS frontend
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM base AS api
COPY pyproject.toml ./
COPY requirements.lock ./
COPY backend/ ./backend/
RUN pip install --no-cache-dir -c requirements.lock .
COPY --from=frontend /web/dist /app/frontend/dist
ENV VELA_FRONTEND_DIR=/app/frontend/dist
USER 10020:10020
EXPOSE 8080
CMD ["python", "-m", "vela.cli", "api"]

FROM denoland/deno:2.9.8 AS deno
FROM base AS worker
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=deno /usr/bin/deno /usr/local/bin/deno
COPY pyproject.toml ./
COPY requirements.lock ./
COPY backend/ ./backend/
RUN pip install --no-cache-dir -c requirements.lock . 'yt-dlp[default,curl-cffi]==2026.10.26'
ENV HOME=/work DENO_DIR=/work/.deno XDG_CACHE_HOME=/work/.cache
USER 10020:10020
CMD ["python", "-m", "vela.cli", "worker"]

FROM base AS browser
RUN apt-get update && apt-get install -y --no-install-recommends chromium ca-certificates fonts-liberation \
 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml requirements.lock ./
COPY backend/ ./backend/
RUN pip install --no-cache-dir -c requirements.lock '.[browser]'
ENV HOME=/browser-profile VELA_CHROMIUM_EXECUTABLE=/usr/bin/chromium
USER 10020:10020
CMD ["python", "-m", "vela.browser_service"]

FROM worker AS test
USER root
RUN pip install --no-cache-dir '.[test]'
COPY tests/ /app/tests/
USER 10020:10020
RUN python -m pytest -q -o cache_dir=/tmp/pytest-cache
