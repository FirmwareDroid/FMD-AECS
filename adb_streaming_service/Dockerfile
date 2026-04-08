# Builder stage: install build tools and build node modules
FROM ubuntu:24.04 AS builder

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
# Allow controlling apt retry attempts in case of flaky network
ARG APT_RETRIES=3
ENV APT_RETRIES=${APT_RETRIES}

# Install build dependencies required for native modules and git
RUN apt-get -o Acquire::Retries=${APT_RETRIES} update && \
    apt-get -o Acquire::Retries=${APT_RETRIES} install -y --no-install-recommends \
      ca-certificates \
      curl \
      gnupg \
      git \
      build-essential \
      python3 \
      libssl-dev \
      adb \
      unzip \
      jq && \
    rm -rf /var/lib/apt/lists/*

# Copy sources and install all deps (hoisted workspace + source-specific)
COPY . /app
WORKDIR /app

# ---- Install Node.js 22 (NodeSource) ----
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get -o Acquire::Retries=${APT_RETRIES} update && \
    apt-get -o Acquire::Retries=${APT_RETRIES} install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# ---- Install pnpm ----
RUN npm install -g pnpm@latest


# Install and Build all dependencies
ENV CI=true
RUN pnpm recursive install --prefer-offline --no-frozen-lockfile && \
    cd /app/source && \
    pnpm install

# Create unprivileged user
RUN groupadd -r appgroup && useradd -r -g appgroup -d /home/app -m -s /bin/bash app
RUN chown -R app:appgroup /app && chmod -R g+rX /app
USER app

WORKDIR /app/source

# Start: run adb devices then pm2-runtime from local node_modules
# Use shell wrapper to run adb devices before pm2 runtime (pm2 binary is in node_modules/.bin)
CMD ["sh", "-c", "pnpm build-start"]
