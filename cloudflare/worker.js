/**
 * Cloudflare Worker Edge Shield for VextOpenBank API
 * 
 * ZERO-TRUST INTERNAL LOCKDOWN:
 * 1. Blocks all external public traffic lacking internal authorization key
 * 2. Injects cryptographic Origin Shield header to backend
 * 3. Enforces strict HSTS, clickjacking, and MIME sniffing protection
 * 4. Strips all backend fingerprinting headers
 */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const clientIP = request.headers.get("CF-Connecting-IP") || "unknown";

    // 1. Strict Internal Authentication & Route-Aware Access Gate
    const authHeader = request.headers.get("Authorization") || "";
    const internalKeyHeader = request.headers.get("X-Vext-Key") || "";
    const envoismsSecret = request.headers.get("X-EnvoiSMS-Secret") || "";
    const expectedKey = env.INTERNAL_API_KEY || "vext_internal_live_key_9f8e7d6c5b4a";
    const expectedEnvoiSecret = env.ENVOISMS_SECRET || "cf_shield_origin_auth_837291048576";

    // Public endpoints that require zero authentication (e.g. read-only bank instructions)
    const isPublicRoute = url.pathname === "/api/v1/envoisms/instructions";

    const isAuthorized =
      isPublicRoute ||
      internalKeyHeader === expectedKey ||
      authHeader === `Bearer ${expectedKey}` ||
      authHeader === expectedKey ||
      (url.pathname.startsWith("/api/v1/envoisms") && (envoismsSecret === expectedEnvoiSecret || envoismsSecret === expectedKey));

    if (!isAuthorized) {
      return new Response(
        JSON.stringify({
          error: "Unauthorized",
          detail: "Access restricted to authorized internal connections only. Valid X-Vext-Key or EnvoiSMS authorization required.",
        }),
        {
          status: 401,
          headers: {
            "Content-Type": "application/json",
            "WWW-Authenticate": "Bearer realm=\"VextInternal\"",
          },
        }
      );
    }

    // 2. Prepare proxied request to origin with Origin Shield Token
    const originUrl = new URL(url.pathname + url.search, env.BACKEND_ORIGIN || "https://vext-open-bank.onrender.com");
    const outgoingHeaders = new Headers(request.headers);
    
    // Inject Cloudflare trace identity to origin
    outgoingHeaders.set("X-Forwarded-For", clientIP);
    outgoingHeaders.set("X-Forwarded-Proto", "https");

    // Cryptographic Origin Shield token so backend only trusts Cloudflare
    outgoingHeaders.set("X-Origin-Shield", env.ORIGIN_SHIELD_SECRET || "cf_shield_origin_auth_837291048576");

    const modifiedRequest = new Request(originUrl.toString(), {
      method: request.method,
      headers: outgoingHeaders,
      body: request.body,
      redirect: "follow",
    });

    try {
      const response = await fetch(modifiedRequest);

      // 3. Response Scrubbing: Strip internal server signatures and cookies
      const responseHeaders = new Headers(response.headers);
      responseHeaders.delete("Server");
      responseHeaders.delete("server");
      responseHeaders.delete("x-powered-by");
      responseHeaders.delete("x-volterra-location");
      responseHeaders.delete("rndr-id");
      responseHeaders.delete("x-render-origin-server");
      
      // Ensure HSTS, anti-sniff, and framing protection headers
      responseHeaders.set("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload");
      responseHeaders.set("X-Content-Type-Options", "nosniff");
      responseHeaders.set("X-Frame-Options", "DENY");
      responseHeaders.set("Referrer-Policy", "strict-origin-when-cross-origin");

      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: responseHeaders,
      });
    } catch (err) {
      return new Response(
        JSON.stringify({
          error: "Gateway Unavailable",
          detail: "Unable to connect to internal origin.",
        }),
        {
          status: 502,
          headers: { "Content-Type": "application/json" },
        }
      );
    }
  },
};
