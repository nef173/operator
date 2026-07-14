import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Minimal production server bundle (.next/standalone) — only the runtime files this app
  // needs. This is what keeps the Railway web container small + low-RAM (the Docker image
  // copies just .next/standalone + .next/static instead of all of node_modules).
  output: "standalone",
  // Pin the workspace root so Turbopack doesn't misinfer it from a stray
  // ~/package-lock.json — that misinference broke its persistent-cache SST writes,
  // which left build-manifest.json ungenerated and every route returning 500.
  turbopack: {
    root: __dirname,
  },
  experimental: {
    // Turbopack's persistent FileSystem cache (default-on since 16.1) was failing to
    // write its SST files here ("Persisting failed: Unable to write SST file"), which
    // left the dev manifests ungenerated and every route 500'ing. Disable it for dev.
    turbopackFileSystemCacheForDev: false,
  },
};

export default nextConfig;
