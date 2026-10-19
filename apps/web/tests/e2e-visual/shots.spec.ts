/**
 * Visual QA for Phase 6a. Runs the real built app (with the dev-only /dev/ui page) against the local
 * stack (`make up`, `make deploy-local`, `make sim`, `make trade-demo`) and:
 *   1. signs in for real through the UI (connect -> SIWE) with scripted Anvil wallets;
 *   2. screenshots every 6a page at 360 / 768 / 1440 px in dark and light;
 *   3. fails on serious/critical axe violations and on horizontal scroll;
 *   4. writes docs/screenshots/phase-6a/*.png plus contact sheets.
 * Run: pnpm --filter @wattswap/web shots
 */

import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { type Browser, type BrowserContext, expect, type Page, test } from "@playwright/test";
import { createTestWallet, type TestWallet } from "./test-wallet";

const APP_CHAIN_ID = 31337;
const OUT = resolve(__dirname, "../../../../docs/screenshots/phase-6a");
const AUTH_DIR = mkdtempSync(resolve(tmpdir(), "wattswap-qa-"));
const WIDTHS = [360, 768, 1440] as const;
const THEMES = ["dark", "light"] as const;
type Theme = (typeof THEMES)[number];

const SELLER = 5; // owns the simulated meter, has WATT, trades and earnings
const EMPTY = 9; // never used: empty account

mkdirSync(OUT, { recursive: true });

interface Shot {
  name: string;
  width: number;
  theme: Theme;
  file: string;
}
const shots: Shot[] = [];
const violations: string[] = [];

async function newContext(
  browser: Browser,
  opts: { width: number; theme: Theme; wallet?: TestWallet; storageState?: string },
): Promise<{ ctx: BrowserContext; page: Page }> {
  const ctx = await browser.newContext({
    viewport: { width: opts.width, height: 900 },
    colorScheme: opts.theme,
    ...(opts.storageState ? { storageState: opts.storageState } : {}),
  });
  await ctx.addInitScript((t) => window.localStorage.setItem("theme", t), opts.theme);
  const page = await ctx.newPage();
  if (opts.wallet) await opts.wallet.install(page);
  return { ctx, page };
}

async function connectWallet(page: Page): Promise<void> {
  await page
    .getByRole("button", { name: /^connect wallet$/i })
    .first()
    .click();
  await page
    .getByRole("button", { name: /QA Test Wallet|Browser Wallet|Injected/i })
    .first()
    .click();
}

async function waitForDashboard(page: Page): Promise<void> {
  await expect(page.getByRole("heading", { name: "Dashboard", level: 1 })).toBeVisible({
    timeout: 30_000,
  });
  for (const id of ["card-portfolio", "card-energy", "card-earnings", "card-price"]) {
    await expect(page.getByTestId(id).locator('[aria-busy="true"]')).toHaveCount(0, {
      timeout: 30_000,
    });
  }
}

async function capture(
  page: Page,
  name: string,
  width: number,
  theme: Theme,
  axe = true,
): Promise<void> {
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(350); // let entrance animations settle
  const file = resolve(OUT, `${name}-${width}-${theme}.png`);
  await page.screenshot({ path: file, fullPage: true });
  shots.push({ name, width, theme, file });

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  if (overflow > 0)
    violations.push(`${name} @${width}/${theme}: horizontal overflow of ${overflow}px`);
  if (axe) {
    const result = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    for (const v of result.violations.filter(
      (x) => x.impact === "serious" || x.impact === "critical",
    )) {
      violations.push(
        `${name} @${width}/${theme}: [${v.impact}] ${v.id}: ${v.help} (${v.nodes.length} nodes) e.g. ${v.nodes[0]?.target.join(" ")}`,
      );
    }
  }
}

test.describe.configure({ mode: "serial" });

// ----------------------------------------------------------------------------------- real sign-in
for (const [label, keyIndex] of [
  ["seller", SELLER],
  ["empty", EMPTY],
] as const) {
  test(`real UI sign-in: connect wallet, switch network if needed, sign SIWE (${label})`, async ({
    browser,
  }) => {
    const wallet = createTestWallet({ keyIndex, appChainId: APP_CHAIN_ID });
    const { ctx, page } = await newContext(browser, { width: 1440, theme: "dark", wallet });
    await page.goto("/dashboard");
    await expect(page.getByRole("heading", { name: "Connect your wallet" })).toBeVisible();
    await connectWallet(page);
    await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
    await page
      .locator("#main")
      .getByRole("button", { name: /sign in with ethereum/i })
      .click();
    await waitForDashboard(page);
    await expect(page.getByTestId("user-menu")).toContainText(wallet.address.slice(0, 4));
    // the message the wallet was asked to sign is the documented EIP-4361 template
    const message = wallet.state.signed[0] ?? "";
    expect(message).toContain("wants you to sign in with your Ethereum account:");
    expect(message).toContain(`Chain ID: ${APP_CHAIN_ID}`);
    expect(message).toContain("Sign in to WattSwap (testnet demo).");
    await ctx.storageState({ path: resolve(AUTH_DIR, `${label}.json`) });
    await ctx.close();
  });
}

test("declined signature shows a calm inline message and stays signed out", async ({ browser }) => {
  const wallet = createTestWallet({
    keyIndex: SELLER,
    appChainId: APP_CHAIN_ID,
    rejectSignatures: true,
  });
  const { ctx, page } = await newContext(browser, { width: 1440, theme: "dark", wallet });
  await page.goto("/dashboard");
  await connectWallet(page);
  await page
    .locator("#main")
    .getByRole("button", { name: /sign in with ethereum/i })
    .click();
  await expect(page.getByText("The signature request was declined in your wallet.")).toBeVisible();
  await expect(
    page.locator("#main").getByRole("button", { name: /sign in with ethereum/i }),
  ).toBeEnabled();
  await ctx.close();
});

test("wrong network: dialog appears and one click switches", async ({ browser }) => {
  const wallet = createTestWallet({ keyIndex: SELLER, appChainId: APP_CHAIN_ID });
  const { ctx, page } = await newContext(browser, { width: 1440, theme: "dark", wallet });
  await page.goto("/dashboard");
  await connectWallet(page);
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
  await wallet.userSwitchesChain(1); // the user flips the wallet to Ethereum mainnet
  const dialog = page.getByRole("dialog", { name: "Wrong network" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: /switch to anvil/i }).click();
  await expect(dialog).toBeHidden();
  expect(wallet.state.chainId).toBe(APP_CHAIN_ID);
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
  await ctx.close();
});

test("expired session: banner appears and sign in again restores", async ({ browser }) => {
  const wallet = createTestWallet({
    keyIndex: SELLER,
    appChainId: APP_CHAIN_ID,
    preAuthorized: true,
  });
  const { ctx, page } = await newContext(browser, {
    width: 1440,
    theme: "dark",
    wallet,
    storageState: resolve(AUTH_DIR, "seller.json"),
  });
  await page.goto("/dashboard");
  await waitForDashboard(page);
  await ctx.clearCookies({ name: "wattswap_session" }); // the server stops recognising the session
  await page.getByTestId("card-portfolio").scrollIntoViewIfNeeded();
  await page.evaluate(() => window.dispatchEvent(new Event("focus"))); // re-check on focus
  await expect(page.getByText("Your session expired")).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: "Sign in again" }).first().click();
  await waitForDashboard(page);
  await expect(page.getByText("Your session expired")).toBeHidden();
  await ctx.close();
});

// -------------------------------------------------------------------------------------- the sweep
for (const width of WIDTHS) {
  for (const theme of THEMES) {
    test(`screenshots ${width}px ${theme}`, async ({ browser }) => {
      const state = (who: string): string => resolve(AUTH_DIR, `${who}.json`);

      // landing
      {
        const { ctx, page } = await newContext(browser, { width, theme });
        await page.goto("/");
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await expect(page.getByTestId("card-price").locator('[aria-busy="true"]')).toHaveCount(0, {
          timeout: 30_000,
        });
        await capture(page, "landing", width, theme);
        await ctx.close();
      }
      // dashboard, signed out
      {
        const { ctx, page } = await newContext(browser, { width, theme });
        await page.goto("/dashboard");
        await expect(page.getByRole("heading", { name: "Connect your wallet" })).toBeVisible();
        await capture(page, "dashboard-signed-out", width, theme);
        await ctx.close();
      }
      // dashboard, signed in with real data / empty account
      for (const [name, who, keyIndex] of [
        ["dashboard-data", "seller", SELLER],
        ["dashboard-empty", "empty", EMPTY],
      ] as const) {
        const wallet = createTestWallet({
          keyIndex,
          appChainId: APP_CHAIN_ID,
          preAuthorized: true,
        });
        const { ctx, page } = await newContext(browser, {
          width,
          theme,
          wallet,
          storageState: state(who),
        });
        await page.goto("/dashboard");
        await waitForDashboard(page);
        await expect(page.getByTestId("connection-status")).toHaveAttribute("data-status", "live", {
          timeout: 20_000,
        });
        await capture(page, name, width, theme);
        await ctx.close();
      }
      // dashboard, API error
      {
        const wallet = createTestWallet({
          keyIndex: SELLER,
          appChainId: APP_CHAIN_ID,
          preAuthorized: true,
        });
        const { ctx, page } = await newContext(browser, {
          width,
          theme,
          wallet,
          storageState: state("seller"),
        });
        await page.route("**/api/v1/portfolio**", (route) =>
          route.fulfill({
            status: 503,
            contentType: "application/json",
            body: JSON.stringify({
              error: { code: "unavailable", message: "database is restarting", request_id: "qa" },
            }),
          }),
        );
        await page.goto("/dashboard");
        await expect(
          page.getByRole("alert").filter({ hasText: "Could not load energy totals" }),
        ).toBeVisible({ timeout: 30_000 });
        await capture(page, "dashboard-error", width, theme);
        await ctx.close();
      }
      // dashboard, loading
      {
        const wallet = createTestWallet({
          keyIndex: SELLER,
          appChainId: APP_CHAIN_ID,
          preAuthorized: true,
        });
        const { ctx, page } = await newContext(browser, {
          width,
          theme,
          wallet,
          storageState: state("seller"),
        });
        await page.route("**/api/v1/portfolio**", async (route) => {
          await new Promise((r) => setTimeout(r, 20_000));
          await route.abort().catch(() => undefined);
        });
        await page.goto("/dashboard");
        await expect(page.getByRole("heading", { name: "Dashboard", level: 1 })).toBeVisible({
          timeout: 30_000,
        });
        await expect(page.getByTestId("card-energy").locator('[aria-busy="true"]')).toHaveCount(1);
        await capture(page, "dashboard-loading", width, theme);
        await ctx.close();
      }
      // component inventory
      {
        const { ctx, page } = await newContext(browser, { width, theme });
        await page.goto("/dev/ui");
        await expect(page.getByTestId("dev-ui")).toBeVisible();
        await capture(page, "dev-ui", width, theme);
        await ctx.close();
      }
    });
  }
}

test("contact sheets and the accessibility/overflow verdict", async ({ browser }) => {
  for (const width of WIDTHS) {
    for (const theme of THEMES) {
      const mine = shots.filter((s) => s.width === width && s.theme === theme);
      if (mine.length === 0) continue;
      const cell = width === 360 ? 300 : width === 768 ? 380 : 560;
      const html = `<body style="margin:0;background:#888;font:12px sans-serif"><div style="display:flex;flex-wrap:wrap;gap:12px;padding:12px;align-items:flex-start">${mine
        .map(
          (s) =>
            `<figure style="margin:0;width:${cell}px"><figcaption style="color:#fff;padding:2px 0">${s.name} · ${width}px · ${theme}</figcaption><img src="file:///${s.file.replace(/\\/g, "/")}" style="width:${cell}px"></figure>`,
        )
        .join("")}</div></body>`;
      const tmp = resolve(AUTH_DIR, `contact-${width}-${theme}.html`);
      writeFileSync(tmp, html);
      const ctx = await browser.newContext({
        viewport: { width: Math.min(cell * 4 + 60, 2400), height: 900 },
      });
      const page = await ctx.newPage();
      await page.goto(`file:///${tmp.replace(/\\/g, "/")}`);
      await page.waitForLoadState("load");
      await page.screenshot({
        path: resolve(OUT, `contact-${width}-${theme}.png`),
        fullPage: true,
      });
      await ctx.close();
    }
  }
  expect(violations, violations.join("\n")).toEqual([]);
});
