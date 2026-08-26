# grayson knowledge appliance: read-only team-library MCP over HTTP.
# No warehouse credentials in this image — the only secrets are the qa-library
# deploy key (mounted) and GRAYSON_MCP_TOKEN. See docs/DEPLOYMENT.md recipe 2.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src

RUN useradd --create-home grayson
USER grayson
WORKDIR /home/grayson
RUN mkdir -p .ssh && chmod 700 .ssh \
    && printf 'Host *\n  StrictHostKeyChecking accept-new\n' > .ssh/config

EXPOSE 8850
# GRAYSON_LIBRARY_URL: git URL of the team qa-library (deploy key mounted at
# /home/grayson/.ssh/id_ed25519). GRAYSON_MCP_TOKEN: the bearer token clients
# must present (picked up by `--token`'s envvar).
ENTRYPOINT ["/bin/sh", "-c", \
    "exec grayson mcp serve --knowledge-only --http --host 0.0.0.0 --port 8850 --library \"$GRAYSON_LIBRARY_URL\""]
