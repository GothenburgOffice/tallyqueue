# syntax=docker/dockerfile:1
FROM python:3.13.18-slim-trixie AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt

# Tests, linter and a server that restarts when the source changes. compose.dev.yaml
# bind-mounts the source over /app.
FROM dependencies AS development
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY . .
ENV DATA_DIR=/data PORT=8000 TALLYQUEUE_RELOAD=1
EXPOSE 8000
CMD ["python", "-m", "tallyqueue"]

# The VS Code dev container only: development plus tools for working inside it. gh comes from
# GitHub's signed apt repository. /app is a bind mount from the host, so git would otherwise
# refuse it as owned by another user.
FROM development AS devcontainer
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && install -d /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends gh git jq less openssh-client procps ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory /app

FROM dependencies AS production
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app /data
COPY tallyqueue ./tallyqueue
COPY LICENSE README.md ./
ENV DATA_DIR=/data PORT=8000
LABEL org.opencontainers.image.source="https://github.com/GothenburgOffice/tallyqueue" \
      org.opencontainers.image.title="TallyQueue" \
      org.opencontainers.image.description="A to-do list of receipts and invoices to book. Not a ledger." \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]
CMD ["python", "-m", "tallyqueue"]
