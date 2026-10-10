# syntax=docker/dockerfile:1
# SPOTlight operator console (Next.js 16), port 3000. Build context is THIS folder:
#
#   docker build -t spot-web web
#   docker run --rm -p 3000:3000 -e BACKEND_URL=http://host.docker.internal:8000/api/v1 spot-web
#
# The console holds no data of its own: every number comes from the backend API over HTTP
# (BACKEND_URL), so this image needs no database URL and no model. See web/src/lib/backend.ts.
#
# The builder stage needs network: next/font/google fetches Fira Sans and Fira Code at build time
# (web/src/app/layout.tsx). Nothing else here reaches the internet, at build or at runtime.
FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:24-alpine AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules node_modules
COPY . .
# `output: "standalone"` (next.config.ts) traces the server into .next/standalone.
RUN npm run build

FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0 \
    BACKEND_URL=http://backend:8000/api/v1

# The traced bundle; `public` and `.next/static` are not copied into it by the tracer.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

RUN addgroup -g 10001 -S app && adduser -u 10001 -S app -G app
USER app

EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/api/health').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"

CMD ["node", "server.js"]
