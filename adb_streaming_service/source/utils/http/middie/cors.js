/**
 * CORS middleware for uWebSockets.js routes.
 *
 * Configurable via the CORS_ORIGIN environment variable.
 * Set CORS_ORIGIN to a specific origin (e.g. "https://example.com") to restrict
 * cross-origin access, or leave it unset to allow any origin.
 *
 * NOTE: Credentials are always allowed; in production set CORS_ORIGIN to a specific
 * origin rather than keeping the default "*" wildcard.
 */
export const cors = (res, req) => {
    const allowedOrigin = process.env.CORS_ORIGIN || req.getHeader("origin") || "*";
    res.setHeader("Access-Control-Allow-Origin", allowedOrigin);
    res.setHeader("Access-Control-Allow-Credentials", "true");
    res.setHeader(
        "Access-Control-Allow-Headers",
        "x-api-key, Origin, X-Requested-With, Content-Type, Accept, Cache-Control, Authorization",
    );
    res.setHeader(
        "Access-Control-Allow-Methods",
        "GET,HEAD,POST,PUT,DELETE,OPTIONS",
    );
};
