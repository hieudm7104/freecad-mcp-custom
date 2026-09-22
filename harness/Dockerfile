# Build context is the repo root (frontend/ and harness/ are both needed).

# The React chat + preview page. It is served by the harness, so there is no
# separate frontend container and no second origin for the browser to hit.
FROM node:22-slim AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# node 22, not 20: both pi packages declare engines node>=22.19.0.
FROM node:22-slim AS build
WORKDIR /src
COPY harness/package.json harness/package-lock.json ./
RUN npm ci
COPY harness/tsconfig.json ./
COPY harness/src ./src
RUN npm run build

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production
COPY harness/package.json harness/package-lock.json ./
# ~135 MB even here: pi-ai hard-depends on the openai, anthropic, google and
# bedrock SDKs whichever provider is configured.
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /src/dist ./dist
COPY --from=frontend /src/dist ./public
EXPOSE 8000
CMD ["node", "dist/index.js"]
