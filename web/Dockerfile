# Multi-stage build for the Next.js 15 App Router frontend (§2).
# deps -> build -> runner; the runner ships only the `standalone` output and runs
# as a non-root user.  docker-compose.dev.yml targets the `build` stage instead,
# because `next dev` needs the full node_modules the runner deliberately omits.

# ---------------------------------------------------------------- deps
FROM node:24-alpine AS deps
WORKDIR /app
# Copy manifests only, so a source edit does not re-resolve the dependency tree.
COPY package.json package-lock.json* ./
# `npm ci` is reproducible and is what runs once package-lock.json is committed;
# the fallback exists only for the first build of a fresh checkout.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# ---------------------------------------------------------------- build
FROM node:24-alpine AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# `public/` may not exist yet; the runner stage copies it unconditionally.
RUN mkdir -p public && npm run build

# ---------------------------------------------------------------- runner
FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup --system --gid 10001 nodejs \
 && adduser --system --uid 10001 --ingroup nodejs nextjs

# `output: 'standalone'` emits server.js plus the minimal node_modules it needs.
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=build --chown=nextjs:nodejs /app/public ./public

USER nextjs
EXPOSE 3000

CMD ["node", "server.js"]
