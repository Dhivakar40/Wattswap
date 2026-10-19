// The component inventory at /dev/ui exists ONLY in builds made with WEB_DEV_UI=1: its page file is
// named page.dev.tsx, and the `dev.tsx` extension is registered as a page extension only then. A normal
// (production) build therefore does not contain the route at all. CI asserts that.
const devUi = process.env.WEB_DEV_UI === "1";

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  transpilePackages: ["@wattswap/shared"],
  pageExtensions: devUi ? ["tsx", "ts", "dev.tsx"] : ["tsx", "ts"],
  // Directory the build writes to; the visual-QA script builds into its own dir so it never
  // clobbers a developer's `.next`.
  distDir: process.env.WEB_DIST_DIR ?? ".next",
  webpack(config) {
    // RainbowKit imports wagmi's whole connector barrel, which reaches Coinbase's SDK and its optional
    // x402 payment peers. We use none of that; stub the unresolvable optional modules.
    config.resolve.alias = {
      ...config.resolve.alias,
      "@x402/core/client": false,
      "@x402/evm": false,
      "@x402/evm/exact/client": false,
      "@x402/evm/upto/client": false,
      "@x402/svm/exact/client": false,
    };
    return config;
  },
};

export default nextConfig;
