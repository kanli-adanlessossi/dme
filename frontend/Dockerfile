# Image frontend — build multi-stage (G-3).
#
# Stage `build` : compile l'application avec Vite (TypeScript + PWA) vers `dist/`.
# Stage `dev`   : serveur de dev Vite, conservé pour Docker Compose (NFR-12).
#                 La réconciliation du compose (cible `dev`) relève de la sous-tâche 3.4.
# Stage `prod`  : sert `dist/` via nginx:alpine. Dernier stage => cible par défaut
#                 de `docker build` : un build sans `--target` produit l'image de prod.

# --- Stage build : produit dist/ -------------------------------------------
FROM node:20-slim AS build

WORKDIR /app

# Installer les dépendances de façon reproductible à partir du lockfile.
COPY package.json package-lock.json ./
RUN npm ci

# Copier le reste du projet frontend.
COPY . .

# VITE_API_URL est baké dans le bundle au moment du build (variable Vite de
# build-time). Exposée en ARG pour permettre son override à `docker build`,
# avec le même défaut qu'en dev pour rester cohérent.
ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=$VITE_API_URL

RUN npm run build

# --- Stage dev : serveur de dev Vite (conservé pour Compose, NFR-12) --------
FROM node:20-slim AS dev

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm install

COPY . .

EXPOSE 5173

CMD ["npm", "run", "dev"]

# --- Stage prod : nginx sert dist/ (cible par défaut) ----------------------
FROM nginx:alpine AS prod

# Config serveur : placeholder minimal pour que l'image se construise.
# NOTE : le fallback SPA complet, la CSP applicative, les en-têtes de sécurité
# et les règles de cache (court pour sw/manifeste, long immuable pour les assets
# horodatés) sont la responsabilité de la sous-tâche 3.2 — ne pas les écrire ici.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copier le build statique dans la racine web de nginx.
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# CMD par défaut de nginx:alpine (nginx -g 'daemon off;') — pas de surcharge.
