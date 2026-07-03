/// <reference types="vitest" />

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": process.env.CUTAGENT_API_PROXY_TARGET ?? "http://127.0.0.1:8000",
      "/ws": {
        target: process.env.CUTAGENT_API_PROXY_TARGET ?? "http://127.0.0.1:8000",
        ws: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          "react-vendor": ["react", "react-dom", "react-router-dom"],
          "query-vendor": ["@tanstack/react-query"],
          icons: ["lucide-react"],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    globals: true,
  },
});
