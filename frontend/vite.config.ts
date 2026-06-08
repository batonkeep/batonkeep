import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies /api and /ws to the backend so the SPA talks to a single origin,
// mirroring the nginx config used in production (frontend/nginx.conf).
// VITE_API_TARGET overrides the backend origin (e.g. http://localhost:8001) for
// running the SPA against a non-default backend without editing this file.
declare const process: { env: Record<string, string | undefined> };
const apiTarget = process.env.VITE_API_TARGET || "http://localhost:8000";
const wsTarget = apiTarget.replace(/^http/, "ws");

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
      "/ws": { target: wsTarget, ws: true },
    },
  },
});
