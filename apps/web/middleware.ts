import { NextResponse, type NextRequest } from "next/server";

// Listing Agent inside the Shopify admin: Shopify loads our pages in an iframe with
// ?embedded=1&shop=<shop>.myshopify.com&host=… . For those loads we
// - let only that shop's admin frame us (Content-Security-Policy frame-ancestors, required by Shopify),
// - tell the root layout to add Shopify's App Bridge script (x-shopify-shop request header).
const SHOP = /^[a-z0-9][a-z0-9-]*\.myshopify\.com$/;

export function middleware(req: NextRequest) {
  const headers = new Headers(req.headers);
  headers.delete("x-shopify-shop");            // only we set this
  const shop = (req.nextUrl.searchParams.get("shop") || "").toLowerCase();
  const embedded = req.nextUrl.searchParams.get("embedded") === "1" && SHOP.test(shop);
  if (!embedded) return NextResponse.next({ request: { headers } });
  const csp = `frame-ancestors https://${shop} https://admin.shopify.com;`;
  if (req.nextUrl.pathname === "/") {
    // Our App URL is the site root; inside Shopify the app starts on Products (query kept for App Bridge).
    const url = req.nextUrl.clone();
    url.pathname = "/products";
    const res = NextResponse.redirect(url);
    res.headers.set("Content-Security-Policy", csp);
    return res;
  }
  headers.set("x-shopify-shop", shop);
  const res = NextResponse.next({ request: { headers } });
  res.headers.set("Content-Security-Policy", csp);
  return res;
}

export const config = {
  // pages only, not Next's own files
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
