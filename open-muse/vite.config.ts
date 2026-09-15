import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export const API_PROXY_PATTERN = "^/api(?:/|$)";
const appPort = Number(process.env.OPEN_MUSE_E2E_APP_PORT ?? process.env.OPEN_MUSE_PORT ?? 4318);
const clientPort = Number(process.env.OPEN_MUSE_E2E_CLIENT_PORT ?? 5174);

export default defineConfig({
  root: "client",
  plugins: [react()],
  build: { outDir: "../dist/client", emptyOutDir: false },
  server: {
    host: "127.0.0.1",
    port: clientPort,
    proxy: { [API_PROXY_PATTERN]: { target: `http://127.0.0.1:${appPort}`, ws: true } },
  },
});
