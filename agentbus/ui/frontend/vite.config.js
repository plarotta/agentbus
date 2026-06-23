import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output lands in ../static, which FastAPI serves at "/".
// `base: "./"` keeps asset URLs relative so the bundle works under any mount.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
});
