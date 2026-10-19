// Builds the web app WITH the dev-only /dev/ui page (WEB_DEV_UI=1) into its own dist dir, then serves it
// on :3000 (the origin the API's CORS and SIWE_DOMAIN allow). Used by Playwright's webServer; it never
// touches `.next`, so a developer's `next dev` or production build is left alone.
import { spawn, spawnSync } from "node:child_process";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const next = require.resolve("next/dist/bin/next");
const env = { ...process.env, WEB_DEV_UI: "1", WEB_DIST_DIR: ".next-qa" };
const port = process.env.QA_PORT ?? "3000";

if (process.env.QA_SKIP_BUILD !== "1") {
  const build = spawnSync(process.execPath, [next, "build"], { env, stdio: "inherit" });
  if (build.status !== 0) process.exit(build.status ?? 1);
}
const server = spawn(process.execPath, [next, "start", "-p", port], { env, stdio: "inherit" });
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => server.kill(sig));
server.on("exit", (code) => process.exit(code ?? 0));
