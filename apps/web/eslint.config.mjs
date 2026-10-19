import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({ baseDirectory: import.meta.dirname });

const config = [
  {
    ignores: [
      ".next*/**",
      "node_modules/**",
      "next-env.d.ts",
      "test-results/**",
      "playwright-report/**",
      "src/lib/api/schema.d.ts",
    ],
  },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
  {
    // Amounts are bigint through lib/amounts only; colours come from design tokens only.
    files: ["src/components/**/*.{ts,tsx}", "src/lib/hooks/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-globals": ["error", "parseFloat"],
      "no-restricted-syntax": [
        "error",
        {
          selector: "CallExpression[callee.name=/^(Number|parseFloat|parseInt)$/]",
          message: "Do not convert amounts with JS numbers: use bigint and lib/amounts.",
        },
        {
          selector: "Literal[value=/^#[0-9a-fA-F]{3,8}$/]",
          message: "Use a design token (bg-primary, text-muted-foreground, ...), not a hex colour.",
        },
      ],
    },
  },
];

export default config;
