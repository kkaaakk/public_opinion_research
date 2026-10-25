import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/studio/assets/",
  build: {
    outDir: "../src/open_deep_research/web/studio",
    emptyOutDir: true,
    assetsDir: ".",
    manifest: true
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000"
    }
  }
});
