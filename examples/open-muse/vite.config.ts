import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export const API_PROXY_PATTERN = "^/api(?:/|$)";

export default defineConfig({
  root: "client",
  plugins: [react()],
  build: {
    outDir: "../dist/client",
    emptyOutDir: false,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { [API_PROXY_PATTERN]: "http://127.0.0.1:4317" },
  },
});
