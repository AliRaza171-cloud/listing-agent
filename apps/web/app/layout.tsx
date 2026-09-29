import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "Listing Agent — photo in, listing out",
  description: "Upload product photos, say the price, and get store-ready listings in English and Urdu.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#1B1A17",
};

const FONTS =
  "https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800" +
  "&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@500&family=Noto+Nastaliq+Urdu:wght@400;600&display=swap";

// Shopify's App Bridge (for Listing Agent inside the Shopify admin). The client ID is public.
const SHOPIFY_API_KEY = process.env.NEXT_PUBLIC_SHOPIFY_API_KEY || "";

export default async function RootLayout({ children }: { children: ReactNode }) {
  // Set by middleware.ts only when Shopify loads us inside its admin (?embedded=1&shop=…).
  const shopifyShop = (await headers()).get("x-shopify-shop");
  return (
    <html lang="en">
      <head>
        {shopifyShop && SHOPIFY_API_KEY && (
          <>
            {/* App Bridge must be the first script in <head>, loaded normally (no async/defer). */}
            <meta name="shopify-api-key" content={SHOPIFY_API_KEY} />
            {/* eslint-disable-next-line @next/next/no-sync-scripts */}
            <script src="https://cdn.shopify.com/shopifycloud/app-bridge.js" />
          </>
        )}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        {/* eslint-disable-next-line @next/next/no-page-custom-font */}
        <link rel="stylesheet" href={FONTS} />
      </head>
      <body>{children}</body>
    </html>
  );
}
