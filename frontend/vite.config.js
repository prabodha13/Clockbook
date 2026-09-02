import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Forwards /api calls to the FastAPI backend during local development
      "/api": "http://localhost:8000",
    },
  },
});
