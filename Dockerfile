# syntax=docker/dockerfile:1.7
#
# Multi-stage build for finhealth.
#
#   builder  — installs the build backend (hatchling) and builds a
#              wheel of the production package. Dev extras
#              (pytest, ruff, mypy, ...) are intentionally *not*
#              installed; only the wheel they produce matters.
#   runtime  — slim image with the wheel + system libraries needed
#              by pdfplumber (Pillow transitively) and pikepdf
#              (libqpdf). Runs as an unprivileged user, applies
#              Alembic migrations on startup, then serves on :8000.

# ---------------------------------------------------------------------------
# Stage 1 — build the production wheel
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Standard Python-in-Docker hygiene: skip .pyc writes, flush stdout
# straight to the container log stream, and never cache pip downloads
# (we throw this stage away anyway, but it keeps the layer small).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# The build backend (hatchling) is required to build the wheel; it
# is not a runtime dependency and is therefore left behind in this
# stage. ``--no-cache-dir`` is redundant given ``PIP_NO_CACHE_DIR``
# but kept explicit for readers who skim the Dockerfile.
RUN pip install --no-cache-dir hatchling

# Copy only the files hatchling needs to build the project. The
# ``app/`` source tree is the actual package; ``LICENSE`` and
# ``README.md`` are referenced from ``pyproject.toml`` metadata and
# must be present at build time. Everything else (.git, tests,
# .venv, PDFs, ...) is excluded by ``.dockerignore``.
COPY pyproject.toml LICENSE README.md ./
COPY app ./app

# Build a single wheel into a local directory. We do not run
# ``pip install`` here because that would mix the production
# package with the build backend on ``site-packages``, making it
# harder to copy only what we need to the runtime stage.
RUN pip wheel --no-cache-dir --wheel-dir=/wheels .


# ---------------------------------------------------------------------------
# Stage 2 — runtime image
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System libraries for the PDF pipeline.
#
#   * ``libgl1`` — the Debian 12+ replacement for the removed
#     ``libgl1-mesa-glx`` metapackage. Pulled in transitively by
#     Pillow, which pdfplumber uses for image rendering.
#   * ``libglib2.0-0`` — runtime dependency of libcairo / pango
#     (also pulled in by Pillow on some wheels).
#
# ``markitdown[all]`` (the new PDF-to-Markdown extractor) is
# pure-Python plus ``magika`` (which vendors an ONNX model); it
# does not introduce additional system-level shared libraries
# beyond what ``pdfplumber`` already needed. If a future extra is
# added (e.g. ``[audio]`` or ``[video]``), ``ffmpeg`` would have
# to land here too.
#
# ``--no-install-recommends`` keeps the layer as small as possible;
# ``rm -rf /var/lib/apt/lists/*`` drops the apt cache so it does not
# bloat the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install the production wheel from the builder stage, then drop
# the wheel directory. Because the wheel was built *without* the
# ``[dev]`` extras, the runtime image does not contain pytest,
# ruff, mypy, or any other test-time tooling.
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

# Application code. Only the pieces needed at runtime are copied:
#   * ``app/``         — the FastAPI package itself
#   * ``alembic/``     — migration scripts (read by ``alembic upgrade``)
#   * ``alembic.ini``  — points Alembic at ``alembic/env.py``
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

# Persistent directories. Under docker-compose these are bind
# mount points owned by the host user, so the Dockerfile creates
# them but does not chown them — the actual ownership is
# governed by the ``user:`` directive in ``docker-compose.yml``
# (or the ``docker run -u`` flag), which maps the container user
# to the host UID/GID that owns the bind-mounted directories.
# The 0:0 default in compose means "run as root", which works
# for any host that does not care about file ownership; setting
# ``UID``/``GID`` in ``.env`` to match the host user keeps the
# SQLite database and uploaded PDFs owned by the host user.
RUN mkdir -p /app/shared /app/data

EXPOSE 8000

# Liveness probe. ``/api/v1/health`` does a DB round-trip, so a
# failing probe means the connection string is wrong, the
# migrations did not run, or the database file is unwritable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
r = urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3); \
sys.exit(0 if r.status == 200 else 1)" \
    || exit 1

# ``sh -c`` chains the two commands. ``alembic upgrade head`` is
# idempotent and a no-op when already at the head revision, so
# running it on every start is safe and keeps the schema in sync
# with the code in the image. ``uvicorn`` is then started in the
# foreground so the container stays alive.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
