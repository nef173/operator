# Operator web (Next.js 16, standalone output). Multi-stage so the runtime image carries
# only the minimal server bundle — small + low-RAM on Railway.

# ---- deps: install node_modules from the lockfile (cached until package*.json change) ----
FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ---- build: compile the app to .next/standalone ----
FROM node:22-slim AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* is inlined into the client bundle at BUILD time, so the API URL must be
# present here (Railway passes it as a build arg → env). Defaults to localhost for a bare
# `docker build`; set NEXT_PUBLIC_API_BASE to the deployed API URL on Railway.
ARG NEXT_PUBLIC_API_BASE=http://localhost:8077
ENV NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---- runner: copy just the standalone server + static assets ----
FROM node:22-slim AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000
# Run as a non-root user.
RUN groupadd --system --gid 1001 nodejs && useradd --system --uid 1001 --gid nodejs nextjs
# .next/standalone already contains a pruned node_modules + server.js.
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=build --chown=nextjs:nodejs /app/public ./public
USER nextjs
# server.js honors $PORT and binds 0.0.0.0 by default in standalone mode.
CMD ["sh", "-c", "node server.js"]
