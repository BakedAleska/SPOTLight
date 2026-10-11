import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* Traced server bundle for the container (web/Dockerfile): the runtime stage
     copies .next/standalone and needs no node_modules of its own. */
  output: "standalone",
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
