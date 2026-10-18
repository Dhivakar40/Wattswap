import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["src/abis/**"] },
  js.configs.recommended,
  ...tseslint.configs.strict,
  {
    rules: { "@typescript-eslint/no-explicit-any": "error" },
  },
);
