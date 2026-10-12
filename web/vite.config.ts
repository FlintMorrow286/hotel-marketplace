import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// Single-app: in dev the API is proxied to FastAPI so the browser sees one
// origin; in production Vite builds static assets that FastAPI serves.
// The port comes from VITE_API_PORT (web/.env.local) — several projects share
// this machine, so a hard-coded port silently reaches the wrong server and
// the catalog comes back empty. process.env is not populated from the env
// files while the config itself is still loading, hence the explicit loadEnv.
const env = loadEnv(process.env.NODE_ENV ?? "development", __dirname, "");
const apiPort = env.VITE_API_PORT ?? process.env.VITE_API_PORT ?? "8000";
const apiOrigin = `http://localhost:${apiPort}`;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/v1": {
        target: apiOrigin,
        changeOrigin: true,
      },
      "/media": apiOrigin,
      "/healthz": apiOrigin,
      "/readyz": apiOrigin,
    },
  },
  // web/public holds the room photos (served as-is, /rooms/...).
  publicDir: "public",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  test: {
    exclude: [
      "**/node_modules/**",
      "**/dist/**",
      "**/cypress/**",
      "**/.{idea,git,cache,output,temp}/**",
      "**/e2e/**",
    ],
  },
});
