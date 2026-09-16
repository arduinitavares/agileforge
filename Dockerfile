FROM ghcr.io/astral-sh/uv:0.12.8@sha256:d82243909a4a5d6360fe707db12247554d05f143938aabea2017a91477e83e24 AS uv

FROM ubuntu:24.04@sha256:496754492fb28b4d3049432f2ca787449331e23fb14f0dd3fffea86bf5a93eb4 AS certificate-bootstrap
# The immutable Ubuntu base has no CA bundle. Its default Ubuntu archive uses
# signed APT metadata, so it can bootstrap this exact CA package before the
# snapshot stages make their TLS-only requests.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
       ca-certificates=20240203 \
    && rm -rf /var/lib/apt/lists/*

FROM ubuntu:24.04@sha256:496754492fb28b4d3049432f2ca787449331e23fb14f0dd3fffea86bf5a93eb4 AS runtime-base
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=certificate-bootstrap /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
RUN printf '%s\n' 'Types: deb' \
    'URIs: https://snapshot.ubuntu.com/ubuntu/20260916T000000Z/' \
    'Suites: noble noble-updates noble-security' \
    'Components: main universe restricted multiverse' \
    'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
    > /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
       ca-certificates curl git tini xz-utils libatomic1 \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_TOOL_DIR=/opt/tools \
    UV_TOOL_BIN_DIR=/opt/bin \
    UV_CACHE_DIR=/cache/uv \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1 \
    PATH=/opt/bin:/opt/python/cpython-3.13.15-linux-x86_64-gnu/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
RUN uv python install 3.13.15 \
    && groupadd --gid 10001 agileforge \
    && useradd --uid 10001 --gid 10001 --create-home agileforge \
    && mkdir -p /workspace /cache /var/lib/agileforge /opt/agileforge \
    && chown -R 10001:10001 /workspace /cache /var/lib/agileforge

FROM runtime-base AS development
RUN apt-get -o Acquire::Check-Valid-Until=false update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
       build-essential procps time sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && curl --fail --silent --show-error --location \
       https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.xz \
       --output /tmp/node.tar.xz \
    && printf '%s\n' 'fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6  /tmp/node.tar.xz' | sha256sum --check \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && uv tool install --python 3.13.15 \
       'git+https://github.com/arduinitavares/pyrepo-check.git@ac41bbe8e8588b8f4232979c47d26be37d78d413' \
    && chown -R 10001:10001 /cache
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]

FROM development AS test
USER root
COPY pyproject.toml uv.lock /opt/test-dependencies/
RUN uv sync --project /opt/test-dependencies --locked --no-install-project \
    && /opt/test-dependencies/.venv/bin/python -m playwright install --with-deps chromium \
    && mv /root/.cache/ms-playwright /opt/ms-playwright \
    && chmod -R a+rX /opt/ms-playwright \
    && chown -R 10001:10001 /cache
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
USER 10001:10001

FROM runtime-base AS wheel-builder
WORKDIR /build
COPY pyproject.toml uv.lock README.md agile_sqlmodel.py api.py /build/
COPY containers /build/containers
COPY adapters /build/adapters
COPY cli /build/cli
COPY config /build/config
COPY db /build/db
COPY frontend /build/frontend
COPY models /build/models
COPY repositories /build/repositories
COPY routers /build/routers
COPY services /build/services
COPY tools /build/tools
COPY utils /build/utils
COPY workflow /build/workflow
RUN uv sync --locked --no-install-project --no-editable \
    && uv build --wheel --out-dir /wheels \
    && uv pip install --python /build/.venv/bin/python --no-deps /wheels/*.whl

FROM runtime-base AS production
ARG SOURCE_REVISION
ARG SOURCE_SHA256
ARG LOCK_SHA256
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
LABEL io.agileforge.source-sha256=$SOURCE_SHA256
LABEL io.agileforge.lock-sha256=$LOCK_SHA256
COPY --from=wheel-builder /build/.venv /build/.venv
COPY --from=wheel-builder /build/containers/build.json /opt/agileforge/build.json
RUN chmod 0444 /opt/agileforge/build.json
ENV PATH=/build/.venv/bin:/usr/local/bin:/usr/bin:/bin
USER 10001:10001
WORKDIR /var/lib/agileforge
ENTRYPOINT ["/usr/bin/tini", "--", "/build/.venv/bin/python", "-m", "cli.container_runtime"]
CMD ["serve", "--profile", "default"]
