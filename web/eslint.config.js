import js from "@eslint/js";
import globals from "globals";
import accessibility from "eslint-plugin-jsx-a11y";
import hooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/**", ".npm-cache/**"] },
  js.configs.recommended,
  tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: { globals: globals.browser },
    plugins: { "react-hooks": hooks, "jsx-a11y": accessibility },
    rules: {
      ...hooks.configs.recommended.rules,
      ...accessibility.configs.recommended.rules,
    },
  },
);
