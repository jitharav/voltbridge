import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// GitHub Pages serves this repo under /voltbridge/, so assets must be based there.
// For a custom domain at the root later, change base back to "/".
export default defineConfig({
  base: "/voltbridge/",
  plugins: [react()],
});
