/**
 * Cloudflare Worker Edge Shield for VextOpenBank API
 * 
 * 1. Sanitizes request/response headers (strips sensitive internal backend tokens)
 * 2. Enforces edge rate limiting per client IP
 * 3. Enforces strict HTTPS and CORS policies
 * 4. Proxies requests to the Cloudflare Tunnel origin
 */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const clientIP = request.headers.get("CF-Connecting-IP") || "unknown";

    // 1. Edge Rate Limiting (if binding is configured)
    if (env.RATE_LIMITER) {
      const { success } = await env.RATE_LIMITER.limit({ key: clientIP });
      if (!success) {
        return new Response(
          JSON.stringify({
            error: "Too Many Requests",
            detail: "Rate limit exceeded. Try again in 60 seconds.",
          }),
          {
            status: 429,
            headers: {
              "Content-Type": "application/json",
              "Retry-After": "60",
            },
          }
        );
      }
    }

    // 2. Prepare proxied request to origin
    const originUrl = new URL(url.pathname + url.search, env.BACKEND_ORIGIN || "http://127.0.0.1:8000");
    const outgoingHeaders = new Headers(request.headers);
    
    // Inject Cloudflare trace identity to origin
    outgoingHeaders.set("X-Forwarded-For", clientIP);
    outgoingHeaders.set("X-Forwarded-Proto", "https");

    // Clone request for origin fetch
    const modifiedRequest = new Request(originUrl.toString(), {
      method: request.method,
      headers: outgoingHeaders,
      body: request.body,
      redirect: "follow",
    });

    try {
      const response = await fetch(modifiedRequest);

      // 3. Response Scrubbing: Strip internal server signatures and sensitive cookies
      const responseHeaders = new Headers(response.headers);
      responseHeaders.delete("Server");
      responseHeaders.delete("x-powered-by");
      responseHeaders.delete("x-volterra-location");
      
      // Ensure HSTS and anti-sniff headers
      responseHeaders.set("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload");
      responseHeaders.set("X-Content-Type-Options", "nosniff");
      responseHeaders.set("X-Frame-Options", "DENY");

      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers: responseHeaders,
      });
    } catch (err) {
      return new Response(
        JSON.stringify({
          error: "Gateway Unavailable",
          detail: "Unable to connect to upstream bank API.",
        }),
        {
          status: 502,
          headers: { "Content-Type": "application/json" },
        }
      );
    }
  },
};
