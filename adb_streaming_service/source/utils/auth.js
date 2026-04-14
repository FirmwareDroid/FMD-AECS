import { createHmac, timingSafeEqual } from 'node:crypto';
import { logger } from '../services/logger.js';

/**
 * Returns true when HTTP Basic authentication is enabled via env vars.
 */
export function isAuthEnabled() {
    return process.env.AUTH_ENABLED === 'true' || process.env.AUTH_ENABLED === '1';
}

/**
 * Validate a "Basic <base64>" authorization header value against the credentials
 * stored in AUTH_USER / AUTH_PASS environment variables.
 *
 * Uses crypto.timingSafeEqual to prevent timing-oracle attacks.
 *
 * @param {string|null} authHeader
 * @returns {boolean}
 */
export function validateBasicAuthHeader(authHeader) {
    if (!authHeader) return false;
    let header = String(authHeader).trim();

    // Normalise a bare base64 payload to "Basic <payload>"
    if (!header.includes(' ')) {
        if (/^[A-Za-z0-9+/=]+$/.test(header)) {
            header = `Basic ${header}`;
        } else {
            return false;
        }
    }

    const parts = header.split(' ');
    if (parts.length !== 2) return false;
    if (!/^Basic$/i.test(parts[0])) return false;

    let decoded;
    try {
        decoded = Buffer.from(parts[1], 'base64').toString('utf8');
    } catch (e) {
        return false;
    }

    const idx = decoded.indexOf(':');
    if (idx === -1) return false;
    const user = decoded.slice(0, idx);
    const pass = decoded.slice(idx + 1);

    const expectedUser = process.env.AUTH_USER || '';
    const expectedPass = process.env.AUTH_PASS || '';

    // Pad both sides to the same byte length before comparing so that
    // timingSafeEqual does not throw on mismatched lengths.
    const maxLen = Math.max(
        Buffer.byteLength(user, 'utf8'),
        Buffer.byteLength(expectedUser, 'utf8'),
        1,
    );
    const maxPassLen = Math.max(
        Buffer.byteLength(pass, 'utf8'),
        Buffer.byteLength(expectedPass, 'utf8'),
        1,
    );

    const aBuf = Buffer.alloc(maxLen);
    const bBuf = Buffer.alloc(maxLen);
    Buffer.from(user, 'utf8').copy(aBuf);
    Buffer.from(expectedUser, 'utf8').copy(bBuf);

    const cBuf = Buffer.alloc(maxPassLen);
    const dBuf = Buffer.alloc(maxPassLen);
    Buffer.from(pass, 'utf8').copy(cBuf);
    Buffer.from(expectedPass, 'utf8').copy(dBuf);

    return timingSafeEqual(aBuf, bBuf) && timingSafeEqual(cBuf, dBuf);
}

/**
 * Extract / normalise an Authorization header value from multiple possible transports:
 *   - actual Authorization header
 *   - `auth` query parameter (either "Basic <base64>" or just "<base64>")
 *   - origin URL userinfo: user:pass@host
 *
 * @param {object} req - uWebSockets.js HttpRequest (or compatible).
 * @returns {string|null}
 */
export function getAuthHeaderFromRequest(req) {
    try {
        // 1. Authorization header
        const h = req.getHeader ? (req.getHeader('authorization') || req.getHeader('Authorization')) : null;
        if (h) return String(h).trim();

        // 2. auth query param
        try {
            const q = req.getQuery ? req.getQuery('auth') : null;
            if (q) {
                let v = String(q).trim();
                if (/^\s*Basic\s+/i.test(v)) return v;
                if (/^[A-Za-z0-9+/=]+$/.test(v)) return `Basic ${v}`;
                try {
                    const dec = decodeURIComponent(v);
                    if (/^\s*Basic\s+/i.test(dec)) return dec;
                } catch (_) { /* ignore */ }
            }
        } catch (e) { /* ignore query parse errors */ }

        // 3. origin URL userinfo
        try {
            const origin = req.getHeader
                ? (req.getHeader('origin') || req.getHeader('Origin'))
                : null;
            if (origin) {
                try {
                    const u = new URL(origin);
                    if (u.username) {
                        const user = decodeURIComponent(u.username);
                        const pass = decodeURIComponent(u.password || '');
                        const payload = Buffer.from(`${user}:${pass}`, 'utf8').toString('base64');
                        return `Basic ${payload}`;
                    }
                } catch (e) { /* ignore URL parse errors */ }
            }
        } catch (e) { /* ignore */ }

        return null;
    } catch (e) {
        return null;
    }
}

/**
 * Guard an HTTP route: send 401 and return false when auth fails.
 *
 * @param {object} res - Raw uWS response.
 * @param {object} req - Raw uWS request.
 * @returns {boolean} true when the request is authorised (or auth is disabled).
 */
export function requireAuthForHttp(res, req) {
    if (!isAuthEnabled()) return true;
    const authHeader =
        getAuthHeaderFromRequest(req) ||
        (req.getHeader && (req.getHeader('authorization') || req.getHeader('Authorization')));
    if (validateBasicAuthHeader(authHeader)) return true;
    res.writeStatus('401 Unauthorized');
    res.writeHeader('WWW-Authenticate', 'Basic realm="FirmwareDroid"');
    res.end('Unauthorized');
    return false;
}

/**
 * Guard a WebSocket upgrade: respond 401 and return false when auth fails.
 *
 * @param {object} res - Raw uWS response.
 * @param {object} req - Raw uWS request.
 * @returns {boolean}
 */
export function requireAuthForUpgrade(res, req) {
    try {
        if (!isAuthEnabled()) return true;
        const authHeader =
            getAuthHeaderFromRequest(req) ||
            (req.getHeader && (req.getHeader('authorization') || req.getHeader('Authorization')));
        if (validateBasicAuthHeader(authHeader)) return true;
        try {
            res.writeStatus('401 Unauthorized');
            res.writeHeader('WWW-Authenticate', 'Basic realm="FirmwareDroid"');
            res.end('Unauthorized');
        } catch (e) {
            try { res.close(); } catch (ee) { /* ignore */ }
        }
        return false;
    } catch (e) {
        try { logger.error('requireAuthForUpgrade error', e?.message || e); } catch (ex) { /* ignore */ }
        try { res.writeStatus('500 Internal Server Error'); res.end('Internal server error'); } catch (er) { /* ignore */ }
        return false;
    }
}

/**
 * HTTP middleware wrapper for the App.use() pipeline.
 * Allows unauthenticated access to /api/health and OPTIONS preflight requests.
 *
 * @param {object} response - Wrapped Response object.
 * @param {object} request  - Wrapped Request object.
 * @returns {boolean}
 */
export function httpAuthMiddleware(response, request) {
    try {
        // Allow OPTIONS preflight without auth
        try {
            if (request && request.route && String(request.route.method).toLowerCase() === 'options') return true;
        } catch (e) { /* ignore */ }

        if (!isAuthEnabled()) return true;

        // Allow unauthenticated access to health endpoints
        try {
            const routeUrl = request?.route?.url ? String(request.route.url) : null;
            const reqUrl = typeof request?.getUrl === 'function' ? request.getUrl() : null;
            if (routeUrl?.startsWith('/api/health')) return true;
            if (reqUrl && String(reqUrl).startsWith('/api/health')) return true;
        } catch (e) { /* ignore, fall through to auth check */ }

        return requireAuthForHttp(response.res, request.req);
    } catch (e) {
        try { logger.error('httpAuthMiddleware error', e?.message || e); } catch (ex) { /* ignore */ }
        try { response.writeStatus('500 Internal Server Error'); response.end('Internal server error'); } catch (er) { /* ignore */ }
        return false;
    }
}
