import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/admin/",
  build: {
    outDir: "../../static/admin",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/admin/api": {
        target: "http://127.0.0.1:8082",
        changeOrigin: true,
      },
      "/health": {
        target: "http://127.0.0.1:8082",
        changeOrigin: true,
      },
    },
  },
});
