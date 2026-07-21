import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base: "./" makes asset paths relative so the build works both locally
// and when served from a GitHub Pages project subpath (/<repo>/).
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: { port: 5173, open: true },
});
