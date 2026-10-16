# Image frontend : serveur de dev Vite (ossature tâche 1).
FROM node:20-slim

WORKDIR /app

# Installer les dépendances
COPY package.json ./
RUN npm install

# Copier le reste du projet
COPY . .

EXPOSE 5173

CMD ["npm", "run", "dev"]
