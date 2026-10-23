# Bank Hapoalim + Cal (visaCal) one-shot sync image (default entrypoint = Hapoalim; Cal runs
# `--entrypoint dumb-init IMAGE -- node /app/cal-auto.mjs`). Built and pinned by host/setup-host.sh.
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
# Local Cal month-walk patch of visa-cal.js (opt-in `calWalk`, see patches/README.md "Cal"). sha256 checked too.
COPY patches/israeli-bank-scrapers+6.9.0-visa-cal.patch /tmp/visa-cal-walk.patch
RUN echo "420d076ad00b219c54a323cbef42a33c594e3a75465f9fc545a4fb603245d80c  /app/node_modules/israeli-bank-scrapers/lib/scrapers/hapoalim.js" | sha256sum -c - \
    && patch -p1 --forward -d /app < /tmp/hapoalim-otp.patch \
    && echo "e4b1ef8cd51adcedcdfd0a4da164690294f733e2d302e816dc30505a46a7aa49  /app/node_modules/israeli-bank-scrapers/lib/scrapers/hapoalim.js" | sha256sum -c - \
    && rm /tmp/hapoalim-otp.patch \
    && echo "74b50dd0b209013c14e1f521ab55a1ac457fa81cac00c8173669b40ee8180c64  /app/node_modules/israeli-bank-scrapers/lib/scrapers/visa-cal.js" | sha256sum -c - \
    && patch -p1 --forward -d /app < /tmp/visa-cal-walk.patch \
    && echo "020da53f84720ee9ece2a01294b1affa4bf4eb754c6aaab7dca2486174ea0b9b  /app/node_modules/israeli-bank-scrapers/lib/scrapers/visa-cal.js" | sha256sum -c - \
    && rm /tmp/visa-cal-walk.patch \
    && apt-get purge -y patch && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY auto.mjs ./auto.mjs
COPY cal-auto.mjs ./cal-auto.mjs
COPY lib ./lib
RUN mkdir -p /profile && chown node:node /profile && chmod 0700 /profile

USER node
ENTRYPOINT ["dumb-init", "--", "node", "/app/auto.mjs"]
