FROM python:3.13-slim

# Agent CLIs to install, space separated: claude codex gemini (default: none).
# Without an agent, Lookout still scans with readers it already learned and with plugins,
# but it cannot learn new sites or find sources.
ARG AGENTS=""

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        nodejs \
        npm \
        chromium \
        fonts-liberation \
    && npm install -g agent-browser \
    && for a in $AGENTS; do \
         case "$a" in \
           claude) npm install -g @anthropic-ai/claude-code ;; \
           codex)  npm install -g @openai/codex ;; \
           gemini) npm install -g @google/gemini-cli ;; \
           *) echo "Unknown agent: $a (use claude, codex or gemini)" >&2; exit 1 ;; \
         esac; \
       done \
    && rm -rf /var/lib/apt/lists/* /root/.npm

RUN groupadd -r lookout \
    && useradd -r -g lookout -m -d /home/lookout -s /bin/bash lookout

WORKDIR /app

COPY pyproject.toml README.md LICENSE run.py ./
COPY lookout ./lookout
COPY web ./web
COPY plugins/README.md plugins/_example_*.py ./plugins/

RUN mkdir -p /data \
    && chown -R lookout:lookout /app /data

USER lookout

# LOOKOUT_BIND=0.0.0.0 lets Docker's port mapping reach the server; the server still only accepts
# Host: 127.0.0.1:<port> / localhost:<port>, so publish on loopback: -p 127.0.0.1:8780:8780
# Headless mode prints a one-time sign-in link to the container log: docker logs lookout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOOKOUT_DATA_DIR=/data \
    LOOKOUT_PORT=8780 \
    LOOKOUT_BIND=0.0.0.0 \
    LOOKOUT_HEADLESS=1 \
    AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium

VOLUME ["/data"]
EXPOSE 8780

CMD ["python", "run.py", "--headless"]
