import { defineConfig, devices } from "@playwright/test";

const ci = process.env.CI === "true";
const appPort = Number(process.env.OPEN_MUSE_E2E_APP_PORT ?? 4318);
const controlPort = Number(process.env.OPEN_MUSE_E2E_CONTROL_PORT ?? 4319);
const clientPort = Number(process.env.OPEN_MUSE_E2E_CLIENT_PORT ?? 5174);

export default defineConfig({
  testDir: "test/e2e/specs",
  outputDir: "artifacts/playwright-results",
  // The scripted server models the production invariant of one active conversation.
  fullyParallel: false,
  forbidOnly: ci,
  retries: ci ? 1 : 0,
  workers: 1,
  reporter: ci
    ? [["line"], ["html", { outputFolder: "artifacts/playwright-report", open: "never" }]]
    : "list",
  use: {
    baseURL: `http://127.0.0.1:${clientPort}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "npm run dev:e2e:harness",
      url: `http://127.0.0.1:${controlPort}/__e2e/state`,
      reuseExistingServer: !ci,
      timeout: 30_000,
    },
    {
      command: `npm exec vite -- --host 127.0.0.1 --port ${clientPort}`,
      url: `http://127.0.0.1:${clientPort}`,
      reuseExistingServer: !ci,
      timeout: 30_000,
    },
  ],
});
