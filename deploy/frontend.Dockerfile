# PatchProof frontend build stage (方案 A 部署)
# Build context: repository root
FROM node:20-alpine

WORKDIR /app

COPY frontend/package.json frontend/pnpm-lock.yaml ./frontend/
RUN cd frontend && corepack enable && pnpm install --frozen-lockfile

COPY frontend/ ./frontend/

WORKDIR /app/frontend
RUN pnpm build

# dist/ is copied to the shared webroot volume by compose.
