import react from "@vitejs/plugin-react";
import { defineConfig, type PluginOption } from "vite";

/**
 * The deployed Mini App lives under `/app/` behind the same origin that serves
 * `/api`. In development the preview is opened at the proxy's root, so a small
 * middleware sends `/` to the base instead of answering 404.
 */
function rootToBase(base: string): PluginOption {
  return {
    name: "gamas-root-to-base",
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        if (req.url === "/" || req.url === "") req.url = base;
        next();
      });
    },
  };
}

export default defineConfig(({ command }) => ({
  plugins: [react(), ...(command === "serve" ? [rootToBase("/app/")] : [])],
  base: "/app/",
  server: {
    host: "0.0.0.0",
    // The sandbox preview reaches the dev server through its own hostname.
    allowedHosts: [".e2b.app", "localhost"],
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
}));
