/** @type {import('next').NextConfig} */
const nextConfig = {
  // Diekspor jadi file statis (folder out/), lalu disajikan oleh FastAPI milik bot.
  output: "export",
  images: { unoptimized: true },
  trailingSlash: false,
  poweredByHeader: false,
  reactStrictMode: true,
  productionBrowserSourceMaps: false,
};

export default nextConfig;
