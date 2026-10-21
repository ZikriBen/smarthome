FROM node:22-bookworm-slim

ENV PUPPETEER_SKIP_DOWNLOAD=true \
    CHROMIUM_PATH=/usr/bin/chromium \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/cache \
    XDG_CONFIG_HOME=/tmp/config

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates dumb-init \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY sync.mjs ./sync.mjs

RUN mkdir -p /reports && chown -R node:node /app /reports
USER node

ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "sync.mjs"]
