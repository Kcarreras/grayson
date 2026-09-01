# grayson knowledge appliance: read-only team-library MCP over HTTP.
# No warehouse credentials in this image — the only secrets are the qa-library
# deploy key (mounted) and GRAYSON_MCP_TOKEN. See docs/DEPLOYMENT.md recipe 2.
#
# Base: digest-pinned Red Hat UBI 9 (Node.js 22 flavor — the platform-approved
# base; grayson is Python, installed from the UBI AppStream repos on top).
# Every stage builds from the same pinned digest so two builds of one commit
# are the same appliance; Python dependencies install from uv.lock, never
# re-resolved at build time.
ARG BASE_IMAGE=registry.access.redhat.com/ubi9/nodejs-22@sha256:de0ec3cba702e28f2ea3c232e8e1b53aa0acee4d25f5acfb458afe22a7b66709

FROM ${BASE_IMAGE} AS deps
USER 0
RUN dnf install -y --nodocs python3.12 python3.12-pip && dnf clean all
WORKDIR /src
COPY pyproject.toml uv.lock README.md /src/
# uv.lock is the source of truth: export its exact pins (with hashes) and
# install those — `pip install .` would re-resolve the open ranges instead
RUN python3.12 -m pip install --no-cache-dir uv \
    && python3.12 -m uv export --frozen --no-dev --no-emit-project \
        --format requirements.txt -o /src/requirements.txt \
    && python3.12 -m venv /opt/grayson \
    && /opt/grayson/bin/pip install --no-cache-dir -r /src/requirements.txt

FROM deps AS builder
COPY . /src
RUN python3.12 -m uv build --wheel --out-dir /tmp/dist /src \
    && /opt/grayson/bin/pip install --no-cache-dir --no-deps /tmp/dist/*.whl

FROM ${BASE_IMAGE} AS runner
USER 0
RUN dnf install -y --nodocs python3.12 git-core openssh-clients && dnf clean all
COPY --from=builder /opt/grayson /opt/grayson
COPY docker/entrypoint.sh /usr/local/bin/grayson-entrypoint
# HOME + group-0 write access keep the image working on platforms that run
# containers under an arbitrary non-root UID (e.g. OpenShift). SSH client
# config is handled by the entrypoint via GIT_SSH_COMMAND — no ~/.ssh/config
# is baked in, so there is no root-owned file for ssh's permission check to
# reject.
RUN chmod 755 /usr/local/bin/grayson-entrypoint \
    && mkdir -p /home/grayson \
    && chgrp -R 0 /home/grayson && chmod -R g=u /home/grayson
ENV HOME=/home/grayson \
    PATH=/opt/grayson/bin:$PATH
WORKDIR /home/grayson
USER 1001

EXPOSE 8850
# Required: GRAYSON_LIBRARY_URL (qa-library git URL or mounted path) and
# GRAYSON_MCP_TOKEN (bearer token clients present) — the entrypoint refuses to
# start without them (GRAYSON_MCP_NO_TOKEN=1 replaces the token ONLY behind a
# gateway that authenticates every caller). Optional: GRAYSON_DEPLOY_KEY,
# GRAYSON_KNOWN_HOSTS, GRAYSON_PORT. See docker/entrypoint.sh.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["/bin/sh", "-c", "python -c \"import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('GRAYSON_PORT','8850')+'/healthz', timeout=4)\""]
ENTRYPOINT ["/usr/local/bin/grayson-entrypoint"]
