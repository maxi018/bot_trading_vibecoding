/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// En desarrollo, Vite (5173) reenvía /api y /ws a la API del bot (8765).
// En producción, la propia API sirve el build (dashboard/dist): mismo origen, sin CORS.
const API = process.env.QB_API_URL ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": { target: API, changeOrigin: false },
      "/ws": { target: API.replace(/^http/, "ws"), ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
  test: { environment: "jsdom" },
});
