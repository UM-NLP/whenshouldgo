import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The dev server proxies the API to the NON-PRODUCTION backend dev server on loopback
// (backend/scripts/dev_server.py), so the browser sees one origin and no CORS is needed.
const API_TARGET = "http://127.0.0.1:8787";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: { "/v1": API_TARGET, "/healthz": API_TARGET },
  },
  build: { sourcemap: false },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    css: false,
  },
});
