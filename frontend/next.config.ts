import type { NextConfig } from "next";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));

const nextConfig: NextConfig = {
  // Static export: the app is a bundle of files any static host can serve.
  output: "export",
  trailingSlash: true,
  turbopack: {
    root,
  },
  images: {
    unoptimized: true, // required for static export
  },
  env: {
    // Embedded at build time. Set this to the deployed backend's origin when
    // building for a real environment; empty means same-origin/relative.
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? "",
  },
  ...(process.env.NODE_ENV === "development" ? {
    async rewrites() {
      return [{ source: "/api/:path*", destination: `${process.env.FOOTBALL_IQ_DEV_API_URL ?? "http://127.0.0.1:8000"}/api/:path*` }];
    },
  } : {}),
};

export default nextConfig;
