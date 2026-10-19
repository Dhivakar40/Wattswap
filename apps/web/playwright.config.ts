import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.QA_PORT ?? 3000);

export default defineConfig({
  testDir: "./tests/e2e-visual",
  testMatch: /.*\.spec\.ts/,
  timeout: 120_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: { baseURL: `http://localhost:${PORT}`, ...devices["Desktop Chrome"], trace: "off" },
  webServer: {
    command: "node scripts/qa-server.mjs",
    url: `http://localhost:${PORT}`,
    reuseExistingServer: false,
    timeout: 600_000,
    stdout: "pipe",
  },
});
