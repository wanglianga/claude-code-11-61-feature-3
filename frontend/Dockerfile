# Build the Vue 3 console and serve it with nginx, proxying /api to the backend.
FROM node:22-slim AS build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm install --registry=https://registry.npmmirror.com --no-audit --no-fund --cache /tmp/npm-cache
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /frontend/dist /usr/share/nginx/html
EXPOSE 80
