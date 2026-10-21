FROM node:22-bookworm-slim

ENV PUPPETEER_SKIP_DOWNLOAD=true \
    CHROMIUM_PATH=/usr/bin/chromium \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/cache \
    XDG_CONFIG_HOME=/tmp/config \
    AGE_PATH=/usr/bin/age

# age encrypts the browser profile at rest; chromium is the scraper; dumb-init reaps zombies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates dumb-init age \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund --ignore-scripts

# Local OTP patch of israeli-bank-scrapers 6.9.0 (NOT upstream), in patch-package format (applies with
# `npx patch-package` too; plain `patch` here keeps the image free of an extra dependency). The sha256 of the
# file is checked before (pristine 6.9.0) and after (exactly our patch): any drift fails the build.
COPY patches/israeli-bank-scrapers+6.9.0.patch /tmp/hapoalim-otp.patch
RUN apt-get update \
    && apt-get install -y --no-install-recommends patch \
    && echo "420d076ad00b219c54a323cbef42a33c594e3a75465f9fc545a4fb603245d80c  /app/node_modules/israeli-bank-scrapers/lib/scrapers/hapoalim.js" | sha256sum -c - \
    && patch -p1 --forward -d /app < /tmp/hapoalim-otp.patch \
    && echo "92bc1132775528795d2c5520ce4d30f3bb2626ad2ec2fd5f21f924b9ca3abd1f  /app/node_modules/israeli-bank-scrapers/lib/scrapers/hapoalim.js" | sha256sum -c - \
    && rm /tmp/hapoalim-otp.patch \
    && apt-get purge -y patch && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY sync.mjs ./sync.mjs
COPY lib ./lib

RUN mkdir -p /reports /state && chown -R node:node /app /reports /state
USER node

ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "sync.mjs"]
