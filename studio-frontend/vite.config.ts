import { rmSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

// The bundle is mounted by the server-rendered studio.html template, so the
// HTML shell Vite would emit is never served and must not land in the tree.
// Vite's own HTML plugin emits it late, hence enforce: "post" plus a cleanup.
function dropIndexHtml(): Plugin {
  let outDir = "";
  return {
    name: "drop-index-html",
    enforce: "post",
    configResolved(config) {
      outDir = resolve(config.root, config.build.outDir);
    },
    generateBundle(_options, bundle) {
      delete bundle["index.html"];
    },
    closeBundle() {
      rmSync(resolve(outDir, "index.html"), { force: true });
    },
  };
}

export default defineConfig({
  plugins: [react(), dropIndexHtml()],
  build: {
    outDir: "../app/web/static/studio-dist",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: "assets/studio.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
