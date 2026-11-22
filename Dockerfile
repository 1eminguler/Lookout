FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        nodejs \
        npm \
        chromium \
        fonts-liberation \
    && npm install -g agent-browser \
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

# Use system Chromium cleanly as non-root user without downloading a duplicate binary.
# LOOKOUT_BIND=0.0.0.0 allows Docker bridge port mapping while server.py still restricts Host headers
# to 127.0.0.1:<port>/localhost:<port>; publish only on loopback: -p 127.0.0.1:8780:8780
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOOKOUT_DATA_DIR=/data \
    LOOKOUT_PORT=8780 \
    LOOKOUT_BIND=0.0.0.0 \
    BROWSER=true \
    AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium

VOLUME ["/data"]
EXPOSE 8780

CMD ["python", "run.py", "--browser"]
