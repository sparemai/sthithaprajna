import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { devPorts } from "./dev-ports.ts";

// Built assets go straight into the Python package so `sthitaprajna serve` needs no Node at runtime.
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  build: {
    outDir: "../src/sthitaprajna/api/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
  server: devPorts(loadEnv(mode, ".", "STHITAPRAJNA_")),
}));
