import { resolve } from "node:path";
import react from "@vitejs/plugin-react";
import license from "rollup-plugin-license";
import { type Plugin } from "vite";
import { defineConfig } from "vitest/config";

const licenseNotices: Plugin = {
  name: "qcws-third-party-notices",
  apply: "build",
  configResolved(config) {
    const options = config.build.rollupOptions;
    options.plugins = [
      options.plugins,
      license({
        thirdParty: {
          output: resolve(
            config.root,
            config.build.outDir,
            "THIRD_PARTY_NOTICES.txt",
          ),
        },
      }),
    ];
  },
};

export default defineConfig({
  base: "/settings/",
  plugins: [react(), licenseNotices],
  build: {
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/app.js",
        assetFileNames: (asset) =>
          asset.names.some((name) => name.endsWith(".css"))
            ? "assets/app.css"
            : "assets/[name][extname]",
        inlineDynamicImports: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
  },
});
