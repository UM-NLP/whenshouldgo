import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The dev server proxies the API to the NON-PRODUCTION backend dev server on loopback
// (backend/scripts/dev_server.py), so the browser sees one origin and no CORS is needed.
const API_TARGET = "http://127.0.0.1:8787";

// The shell cache is versioned per build; activate deletes caches of other versions.
const SW_VERSION = process.env.SW_VERSION ?? new Date().toISOString().replace(/\D/g, "").slice(0, 14);

export default defineConfig({
  plugins: [react()],
  define: { __SW_VERSION__: JSON.stringify(SW_VERSION) },
  server: {
    host: "127.0.0.1",
    proxy: { "/v1": API_TARGET, "/healthz": API_TARGET },
  },
  build: {
    sourcemap: false,
    // The service worker is a second entry emitted at the site root as /sw.js (unhashed, so the
    // browser can find and update it). The app itself is built as before.
    rollupOptions: {
      input: { main: "index.html", sw: "src/sw.ts" },
      output: {
        entryFileNames: (chunk: { name: string }) => (chunk.name === "sw" ? "sw.js" : "assets/[name]-[hash].js"),
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    css: false,
  },
});
