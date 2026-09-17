# syntax=docker/dockerfile:1
#
# TrapTracker Report in a container.
#
# 3.12 rather than the newest Python: it is what the evaluated corpus was built
# on (README "Requirements"), so a container reproduces the recorded figures.
FROM python:3.12-slim-bookworm

# Which optional extras to install. `web` is the UI and PDF export and is small.
# `web,enrich,detect` adds BioCLIP and RT-DETR — torch, several GB. See the
# README's Docker section.
ARG EXTRAS=web
# Chromium prints reports to PDF. Set to 0 for an image without PDF export; the
# endpoint then answers 503 with the reason, exactly as on a host with no browser.
ARG INSTALL_BROWSER=1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --no-sandbox, for Chromium ONLY and only in this image. Measured 2026-09-15:
# under Docker's default security profile Chromium exits "No usable sandbox!",
# and Debian's setuid chromium-sandbox aborts too. The alternatives loosen the
# WHOLE container (seccomp=unconfined, CAP_SYS_ADMIN), so this was a decided
# trade-off, not a default. What bounds it: the browser runs as the unprivileged
# `ttr` user inside the container; it is pointed only at this server's own
# /print page (the URL is pinned to 127.0.0.1, app.py `report_pdf`); and that
# page's body is nh3-sanitised. Set through Debian's /etc/chromium.d/ hook, which
# the /usr/bin/chromium wrapper sources, so pdf.py — and a host install — keep the
# sandbox.
RUN if [ "$INSTALL_BROWSER" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends chromium fonts-dejavu-core fonts-liberation \
        && rm -rf /var/lib/apt/lists/* \
        && echo 'export CHROMIUM_FLAGS="$CHROMIUM_FLAGS --no-sandbox"' > /etc/chromium.d/zz-container-no-sandbox; \
    fi

# Not root. Nothing here needs it, and the PDF browser is a renderer of
# model-derived text that should not run with it.
RUN useradd --create-home --uid 1000 ttr \
    && mkdir -p /data /cache \
    && chown ttr:ttr /data /cache

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# The CPU wheel index only when torch is actually being installed. The default
# PyPI torch wheel bundles CUDA, several GB this container cannot use; and for a
# `web`-only image a second index would be one more place a package could come
# from for no benefit.
RUN case ",$EXTRAS," in \
        *,enrich,*|*,detect,*) index="--extra-index-url https://download.pytorch.org/whl/cpu" ;; \
        *) index="" ;; \
    esac \
    && pip install $index ".[$EXTRAS]" \
    && rm -rf build

# The worked example's sources (examples/back-garden/README.md). `serve` builds
# the example project from the copy bundled in the package on first start; these
# keep `ttr project import-extract --csv docs/...` working inside the container.
COPY examples ./examples
COPY docs/evaluation/data ./docs/evaluation/data

# Projects live on a volume, never in the image. The mailbox password is NOT set
# here and must never be: a container has no OS credential store, so it arrives
# as TTR_IMAP_PASSWORD[__<PROJECT_ID>] from the environment of `docker run` or
# `docker compose` (projects/credentials.py), and is never baked into a layer.
ENV TTR_PROJECTS_ROOT=/data \
    HF_HOME=/cache/huggingface

USER ttr
EXPOSE 8000

ENTRYPOINT ["ttr"]
# 0.0.0.0 is the bind address: listening on every interface in the container is
# the only way a published port reaches it; `--i-understand-no-auth` is what
# `serve` requires for that. The URL `serve` prints uses localhost instead, since a
# browser cannot open 0.0.0.0. Exposure on the HOST is decided by the port
# mapping — compose.yaml publishes on 127.0.0.1 only, which keeps the
# loopback-only intent.
CMD ["serve", "--host", "0.0.0.0", "--port", "8000", "--i-understand-no-auth"]
