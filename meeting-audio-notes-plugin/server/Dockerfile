FROM node:22-slim
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY src ./src
ENV NODE_ENV=production PORT=8080
EXPOSE 8080
CMD ["node", "src/http.js"]
