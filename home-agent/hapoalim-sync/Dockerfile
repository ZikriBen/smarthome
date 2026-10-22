# Bank Hapoalim one-shot sync image. Built and pinned by host/setup-host.sh.
# Base pinned by digest (node 22, Debian bookworm). Chromium from Debian at build time.
FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d

ENV PUPPETEER_SKIP_DOWNLOAD=true \
    CHROMIUM_PATH=/usr/bin/chromium \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/cache \
    XDG_CONFIG_HOME=/tmp/config \
    NODE_ENV=production \
    NO_UPDATE_NOTIFIER=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates dumb-init fonts-dejavu-core patch \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund --ignore-scripts && npm cache clean --force

# Local single-attempt OTP patch of israeli-bank-scrapers 6.9.0 (NOT upstream). sha256 checked before/after.
COPY patches/israeli-bank-scrapers+6.9.0.patch /tmp/hapoalim-otp.patch
RUN echo "420d076ad00b219c54a323cbef42a33c594e3a75465f9fc545a4fb603245d80c  /app/node_modules/israeli-bank-scrapers/lib/scrapers/hapoalim.js" | sha256sum -c - \
    && patch -p1 --forward -d /app < /tmp/hapoalim-otp.patch \
    && echo "92bc1132775528795d2c5520ce4d30f3bb2626ad2ec2fd5f21f924b9ca3abd1f  /app/node_modules/israeli-bank-scrapers/lib/scrapers/hapoalim.js" | sha256sum -c - \
    && rm /tmp/hapoalim-otp.patch \
    && apt-get purge -y patch && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY auto.mjs ./auto.mjs
COPY lib ./lib
RUN mkdir -p /profile && chown node:node /profile && chmod 0700 /profile

USER node
ENTRYPOINT ["dumb-init", "--", "node", "/app/auto.mjs"]
