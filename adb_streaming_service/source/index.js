import * as uWs from "uWebSockets.js";
// remove early static logger and adb imports to allow .env to be loaded first
// import { logger } from "./services/logger.js";
// import { service as adbTcpService } from "./services/adb/adb-tcp-service.js";
import {Packr, Unpackr} from "msgpackr";
import fs from "node:fs";

import {join, dirname} from "node:path";
import {fileURLToPath} from "node:url";
import {global} from "./state/global.js";
import App from "./utils/http/app.js";
import {routes} from "./routes/index.js";
import {cors} from "./utils/http/middie/cors.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

// Fallback logger to use until the real logger module is imported
const consoleFallback = {
    info: (...args) => console.log('[info]', ...args),
    debug: (...args) => console.debug ? console.debug('[debug]', ...args) : console.log('[debug]', ...args),
    error: (...args) => console.error('[error]', ...args),
    // no warn fallback to avoid unused property warning
};

let logger = consoleFallback; // will be replaced by real logger after env load
let adbTcpService; // will be dynamically imported after env is loaded

// Load env file helper (no external dependency)
function loadEnvFile(filePath, {overwrite = false} = {}) {
    try {
        if (!filePath) return false;
        if (!fs.existsSync(filePath)) return false;
        const content = fs.readFileSync(filePath, {encoding: "utf8"});
        const lines = content.split(/\r?\n/);
        for (let raw of lines) {
            raw = raw.trim();
            if (!raw || raw.startsWith("#")) continue;
            // support: export KEY=VALUE or KEY=VALUE
            if (raw.startsWith("export ")) raw = raw.slice(7).trim();
            const eq = raw.indexOf("=");
            if (eq === -1) continue;
            let key = raw.slice(0, eq).trim();
            let val = raw.slice(eq + 1).trim();
            // remove surrounding quotes
            if ((val.startsWith("\"") && val.endsWith("\"")) || (val.startsWith("'") && val.endsWith("'"))) {
                val = val.slice(1, -1);
            }
            if (overwrite || typeof process.env[key] === "undefined") {
                process.env[key] = val;
            }
        }
        // avoid using logger here; defer logging until logger is available
        return true;
    } catch (e) {
        // fallback to console before logger is ready
        console.error(`Failed to load env file ${filePath}: ${e.message}`);
        return false;
    }
}

// Try to load env from several candidates in order of precedence and pick the first that exists
const envCandidates = [
    process.env.ENV_FILE,
    process.env.DOTENV_PATH,
    join(__dirname, "..", ".env"),
    join(__dirname, "..", "..", "env", "adb_streamer", ".env"),
    join(__dirname, "..", "env", "adb_streamer", ".env"),
];
let envFile = envCandidates.find((p) => p && fs.existsSync(p));
if (!envFile) {
    // fallback to repo root .env path (may not exist)
    envFile = join(__dirname, "..", ".env");
}
loadEnvFile(envFile);

// Now that env vars are loaded, dynamically import logger and adb-tcp-service so they pick up env values
try {
    const loggerModule = await import("./services/logger.js");
    logger = loggerModule.logger;
    logger.info(`Loaded env file: ${envFile}`);
    logger.info(`Env values: SSL=${process.env.SSL} AUTH_ENABLED=${process.env.AUTH_ENABLED} PORT=${process.env.PORT} HOST=${process.env.HOST}`);
    const adbModule = await import("./services/adb/adb-tcp-service.js");
    adbTcpService = adbModule.service;
} catch (e) {
    console.error('Failed to initialize logger or adb service after loading env:', e);
    throw e;
}

// Validate required environment variables depending on feature flags.
(() => {
    const missing = [];
    const sslEnabledEnv = (process.env.SSL === 'true' || process.env.SSL === '1');
    const authEnabledEnv = (process.env.AUTH_ENABLED === 'true' || process.env.AUTH_ENABLED === '1');

    if (sslEnabledEnv) {
        if (!process.env.SSL_KEY_PATH) missing.push('SSL_KEY_PATH');
        if (!process.env.SSL_CERT_PATH) missing.push('SSL_CERT_PATH');
    }

    if (authEnabledEnv) {
        if (!process.env.AUTH_USER) missing.push('AUTH_USER');
        if (!process.env.AUTH_PASS) missing.push('AUTH_PASS');
    }

    if (missing.length > 0) {
        logger.error(`Missing required environment variables: ${missing.join(', ')}.`);
        logger.error('Aborting startup. Please set the missing variables in your .env or environment.');
        process.exit(1);
    }
})();

const msgpackOptions = {
    useRecords: true,
    structuredClone: true,
    bundleStrings: true,
};

const packer = new Packr(msgpackOptions);
const unpacker = new Unpackr(msgpackOptions);

// Serialize Error objects (including non-enumerable properties) into plain objects
function serializeError(err) {
    if (!err) return null;
    try {
        const out = {
            name: err.name || 'Error',
            message: err.message || String(err),
            stack: err.stack || null,
        };
        // include other own properties (e.g., code, errno, cause)
        for (const k of Object.getOwnPropertyNames(err)) {
            if (k === 'name' || k === 'message' || k === 'stack') continue;
            try {
                out[k] = err[k];
            } catch (e) {
                out[k] = `unserializable(${String(e)})`;
            }
        }
        // if err has a cause that's an Error, include it serialized
        if (err.cause && typeof err.cause === 'object') {
            try {
                out.cause = serializeError(err.cause);
            } catch (e) {
                out.cause = String(err.cause);
            }
        }
        return out;
    } catch (e) {
        return {name: 'Error', message: String(err), stack: err && err.stack ? err.stack : null};
    }
}

const host = process.env.HOST || "0.0.0.0";
const port = process.env.PORT || 9001;
const sslEnabled = process.env.SSL === "true" || process.env.SSL === "1";
let sslOptions = null;
if (sslEnabled) {
    const keyPath = process.env.SSL_KEY_PATH;
    const certPath = process.env.SSL_CERT_PATH;
    if (!keyPath || !certPath) {
        logger.error("SSL is enabled but SSL_KEY_PATH or SSL_CERT_PATH is not set. Aborting.");
        process.exit(1);
    }
    if (!fs.existsSync(keyPath) || !fs.existsSync(certPath)) {
        logger.error(`SSL key or cert file not found. key=${keyPath} cert=${certPath}. Aborting.`);
        process.exit(1);
    }
    sslOptions = {
        key_file_name: keyPath,
        cert_file_name: certPath,
    };
    logger.info(`SSL enabled — serving HTTPS/WSS on ${host}:${port}`);
}

global.users = new Map();

// Simple Basic auth helper
function isAuthEnabled() {
    return (process.env.AUTH_ENABLED === "true" || process.env.AUTH_ENABLED === "1");
}

// Accept header-like "Basic xxxx" or just the base64 payload; return true only if credentials match env
function validateBasicAuthHeader(authHeader) {
    if (!authHeader) return false;
    let header = String(authHeader).trim();
    // If it's just a base64 payload (no space, likely only [A-Za-z0-9+/=]), normalize to "Basic <payload>"
    if (!header.includes(' ')) {
        // ensure it looks like base64 (quick heuristic)
        if (/^[A-Za-z0-9+/=]+$/.test(header)) {
            header = `Basic ${header}`;
        } else {
            return false;
        }
    }
    const parts = header.split(' ');
    if (parts.length !== 2) return false;
    const scheme = parts[0];
    const payload = parts[1];
    if (!/^Basic$/i.test(scheme)) return false;
    let decoded;
    try {
        decoded = Buffer.from(payload, 'base64').toString('utf8');
    } catch (e) {
        return false;
    }
    const idx = decoded.indexOf(':');
    if (idx === -1) return false;
    const user = decoded.slice(0, idx);
    const pass = decoded.slice(idx + 1);
    const expectedUser = process.env.AUTH_USER || '';
    const expectedPass = process.env.AUTH_PASS || '';
    // simple constant time compare
    return (user === expectedUser && pass === expectedPass);
}

// Extract/normalize an Authorization header value from multiple possible transports:
//  - actual Authorization header
//  - auth query parameter (either "Basic <base64>" or just "<base64>")
//  - origin URL userinfo: wss://user:pass@host  (parses origin and builds Basic token)
function getAuthHeaderFromRequest(req) {
    try {
        // header first
        const h = req.getHeader ? (req.getHeader('authorization') || req.getHeader('Authorization')) : null;
        if (h) return String(h).trim();

        // query param next
        try {
            const q = req.getQuery ? req.getQuery('auth') : null;
            if (q) {
                let v = String(q).trim();
                // If it's already "Basic ..." return as-is
                if (/^\s*Basic\s+/i.test(v)) return v;
                // If it looks like a base64 payload, normalize
                if (/^[A-Za-z0-9+/=]+$/.test(v)) return `Basic ${v}`;
                // If it was url-encoded "Basic%20..." allow decoding
                try {
                    const dec = decodeURIComponent(v);
                    if (/^\s*Basic\s+/i.test(dec)) return dec;
                } catch (_) {
                }
                // otherwise ignore
            }
        } catch (e) { /* ignore query parse errors */
        }

        // Finally, try to parse origin for userinfo (userinfo may be present: wss://user:pass@host)
        try {
            const origin = req.getHeader ? (req.getHeader('origin') || req.getHeader('Origin')) : null;
            if (origin) {
                // origin may contain ws/wss or http/https; use URL parser
                try {
                    const u = new URL(origin);
                    if (u.username) {
                        const user = decodeURIComponent(u.username);
                        const pass = decodeURIComponent(u.password || '');
                        const payload = Buffer.from(`${user}:${pass}`, 'utf8').toString('base64');
                        return `Basic ${payload}`;
                    }
                } catch (e) {
                    // origin might be a host-only string; ignore parse errors
                }
            }
        } catch (e) { /* ignore */
        }

        return null;
    } catch (e) {
        return null;
    }
}

function requireAuthForHttp(res, req) {
    if (!isAuthEnabled()) return true;
    const authHeader = getAuthHeaderFromRequest(req) || req.getHeader && (req.getHeader('authorization') || req.getHeader('Authorization'));
    if (validateBasicAuthHeader(authHeader)) return true;
    res.writeStatus('401 Unauthorized');
    res.writeHeader('WWW-Authenticate', 'Basic realm="FirmwareDroid"');
    res.end('Unauthorized');
    return false;
}

// HTTP middleware wrapper to be used with app.use(...) — enforces auth on all routes
// except health endpoints (those under '/api/health'). This runs before route handlers.
function httpAuthMiddleware(response, request) {
    try {
        // Allow preflight OPTIONS without auth
        try {
            if (request && request.route && String(request.route.method).toLowerCase() === 'options') return true;
        } catch (e) {}

        // If auth is disabled, allow through
        if (!isAuthEnabled()) return true;

        // Allow unauthenticated access to health endpoints
        try {
            const routeUrl = request && request.route && request.route.url ? String(request.route.url) : null;
            const reqUrl = request && typeof request.getUrl === 'function' ? request.getUrl() : null;
            if (routeUrl && routeUrl.startsWith('/api/health')) return true;
            if (reqUrl && String(reqUrl).startsWith('/api/health')) return true;
        } catch (e) {
            // ignore and fall through to auth check
        }

        // Delegate to existing low-level helper using raw uWS objects
        return requireAuthForHttp(response.res, request.req);
    } catch (e) {
        try { logger.error('httpAuthMiddleware error', e?.message || e); } catch (ex) {}
        // On unexpected errors, deny access
        try {
            response.writeStatus('500 Internal Server Error');
            response.end('Internal server error');
        } catch (e) {}
        return false;
    }
}

// WebSocket upgrade authentication helper — validates Basic auth on raw uWS req/res
function requireAuthForUpgrade(res, req) {
    try {
        if (!isAuthEnabled()) return true;
        // getAuthHeaderFromRequest works with objects exposing getHeader/getQuery (uWS HttpRequest has those)
        const authHeader = getAuthHeaderFromRequest(req) || (req.getHeader && (req.getHeader('authorization') || req.getHeader('Authorization')));
        if (validateBasicAuthHeader(authHeader)) return true;
        // Deny the upgrade with a 401 response and the WWW-Authenticate header
        try {
            res.writeStatus('401 Unauthorized');
            res.writeHeader('WWW-Authenticate', 'Basic realm="FirmwareDroid"');
            res.end('Unauthorized');
        } catch (e) {
            // best-effort: if writing fails, just close the response
            try { res.close(); } catch (ee) {}
        }
        return false;
    } catch (e) {
        try { logger.error('requireAuthForUpgrade error', e?.message || e); } catch (ex) {}
        try {
            res.writeStatus('500 Internal Server Error');
            res.end('Internal server error');
        } catch (er) {}
        return false;
    }
}

const run = async () => {
    const app = new App({
        host,
        port,
        protocol: sslEnabled ? 'https' : 'http',
        sslOptions,
    });
    app.use(cors);
    // Enforce authentication for all HTTP routes except health endpoints
    app.use(httpAuthMiddleware);
    for (const route of routes) {
        app.route(route, {});
    }

    // WebSocket endpoint and HTTP status
    app.server
        .ws('/*', {
            compression: uWs.SHARED_COMPRESSOR,
            maxPayloadLength: 16 * 1024,
            idleTimeout: 0,
            upgrade: async (res, req, context) => {
                if (!requireAuthForUpgrade(res, req)) return;
                res.upgrade(
                    {
                        url: req.getUrl(),
                        id: req.getQuery('id'),
                        device: req.getQuery('device'),
                        audio: ['true', null, undefined].includes(req.getQuery('audio')),
                        audioCodec: req.getQuery('audioCodec') ?? 'raw',
                        audioEncoder: req.getQuery('audioEncoder') ?? undefined,
                        video: ['true', null, undefined].includes(req.getQuery('video')),
                        videoCodec: req.getQuery('videoCodec') ?? 'h264',
                        videoEncoder: req.getQuery('videoEncoder') ?? undefined,
                        videoBitRate: ![null, undefined].includes(req.getQuery('videoBitRate')) ? Number.parseInt(req.getQuery('videoBitRate')) * 1000000 : 4_000_000,
                        displayId: ![null, undefined].includes(req.getQuery('displayId')) ? Number.parseInt(req.getQuery('displayId')) : 0,
                        maxSize: ![null, undefined].includes(req.getQuery('maxSize')) ? Number.parseInt(req.getQuery('maxSize')) : 1280,
                        maxFps: ![null, undefined].includes(req.getQuery('maxFps')) ? Number.parseInt(req.getQuery('maxFps')) : 60,
                    },
                    req.getHeader('sec-websocket-key'),
                    req.getHeader('sec-websocket-protocol'),
                    req.getHeader('sec-websocket-extensions'),
                    context,
                );
            },
            open: async (ws) => {
                try {
                    const {id} = ws;
                    let device = ws.device;
                    if (!id) {
                        logger.error('WebSocket open: missing id query parameter');
                        try {
                            ws.send(packer.pack({media: 'error', message: 'missing id'}), true);
                        } catch (e) {
                        }
                        closeSocket(ws, 'missing id', true);
                        return;
                    }

                    // If device not provided and exactly one device connected, auto-select it
                    if (!device) {
                        try {
                            const devices = await adbTcpService.getDevices();
                            if (Array.isArray(devices) && devices.length === 1) {
                                device = devices[0].serial || devices[0].id || devices[0];
                                logger.info(`Auto-selected single connected device: ${device}`);
                            } else {
                                try {
                                    ws.send(packer.pack({
                                        media: 'error',
                                        message: 'missing device and multiple/zero devices connected'
                                    }), true);
                                } catch (e) {
                                }
                                // do not close socket for recoverable client-side selection issues; client may choose another device
                                return;
                            }
                        } catch (e) {
                            logger.error('Failed to list devices for auto-selection:', e);
                            try {
                                ws.send(packer.pack({
                                    media: 'error',
                                    message: 'missing device and could not list devices'
                                }), true);
                            } catch (e) {
                            }
                            // keep connection open to allow client to retry or provide device later
                            return;
                        }
                    }

                    const user = {ws, client: null, abortController: new AbortController()};
                    global.users.set(id, user);

                    let deviceAdb;
                    try {
                        deviceAdb = await adbTcpService.getDeviceAdb(device);
                    } catch (err) {
                        logger.error(`Failed to get device adb for device='${device}' id='${id}':`, err);
                        try {
                            ws.send(packer.pack({media: 'error', message: `device '${device}' not connected`}), true);
                        } catch (e) {
                        }
                        // do not close socket; client might select another device
                        global.users.delete(id);
                        return;
                    }

                    let startResult;
                    try {
                        startResult = await adbTcpService.start(deviceAdb, user);
                    } catch (err) {
                        const errObj = serializeError(err);
                        logger.error(`Failed to start scrcpy client for device='${device}' id='${id}': ${errObj.message}`);
                        // log full error details at debug level so operator can inspect
                        logger.debug('Failed to start scrcpy client error details:', errObj);
                        try {
                            ws.send(packer.pack({
                                media: 'error',
                                message: 'failed to start client',
                                error: errObj
                            }), true);
                        } catch (e) {
                        }
                        // allow client to retry or pick another device
                        global.users.delete(id);
                        return;
                    }

                    const {client, options} = startResult || {};
                    if (!client) {
                        logger.error('No ADB TCP CLIENT');
                        try {
                            ws.send(packer.pack({media: 'error', message: 'no client'}), true);
                        } catch (e) {
                        }
                        // keep connection open; client can request again
                        global.users.delete(id);
                        return;
                    }
                    user.client = client;
                    // remember the device adb and serial on the user for control ops (e.g., querying volume)
                    try {
                        user.deviceAdb = deviceAdb;
                        user.deviceSerial = device;
                    } catch (e) {
                        logger.debug('Could not set user.deviceAdb/serial', e?.message || e);
                    }

                    // pipe client stdout if available
                    if (client.stdout && typeof client.stdout.pipeTo === 'function') {
                        try {
                            client.stdout.pipeTo(new WritableStream({write: (line) => logger.info(line)}), {signal: user.abortController?.signal || undefined}).catch((e) => {
                                if (user?.abortController?.signal?.aborted) return;
                                logger.error('Error piping client.stdout:', e);
                            });
                        } catch (e) {
                            logger.error('Exception while piping client.stdout:', e);
                        }
                    } else {
                        logger.debug('client.stdout not available, skipping stdout piping');
                    }

                    // clipboard
                    if (options && options.clipboard) {
                        options.clipboard.pipeTo(new WritableStream({
                            write: (message) => {
                                try {
                                    sendToUser(user, packer.pack({media: 'message', message}));
                                } catch (ex) {
                                    logger.error(ex);
                                }
                            }
                        }), {signal: user.abortController?.signal || undefined}).catch((e) => {
                            if (user?.abortController?.signal?.aborted) return;
                            logger.error(e);
                        });
                    }

                    // audio
                    if (client.audioStream) {
                        try {
                            const metadata = await client.audioStream;
                            // store simple audio stats per-user
                            user.audioStats = user.audioStats || {
                                sent: 0,
                                dropped: 0,
                                lastPacketSize: 0,
                                lastPacketTs: null
                            };
                            logger.info(`Audio stream metadata for id=${id}: type=${metadata?.type}`);
                            logger.debug('Audio metadata detail', metadata && typeof metadata === 'object' ? (metadata.stream ? Object.assign({}, metadata, {stream: '<stream>'}) : metadata) : String(metadata));
                            switch (metadata.type) {
                                case 'disabled':
                                    logger.info('AudioStream disabled');
                                    break;
                                case 'errored':
                                    logger.error('AudioStream errored');
                                    try {
                                        sendToUser(user, packer.pack({
                                            media: 'audio_error',
                                            message: 'audio stream errored'
                                        }));
                                    } catch (e) {
                                    }
                                    break;
                                case 'success': {
                                    const audioPacketStream = metadata.stream;
                                    if (!audioPacketStream) {
                                        logger.error('AudioStream success but stream is missing');
                                        try {
                                            sendToUser(user, packer.pack({
                                                media: 'audio_error',
                                                message: 'audio stream missing'
                                            }));
                                        } catch (e) {
                                        }
                                        break;
                                    }
                                    // publish audio metadata to client as well so the client can prepare playback
                                    try {
                                        sendToUser(user, packer.pack({media: 'audio_metadata', packet: metadata}));
                                    } catch (e) {
                                        logger.debug('Failed to send audio_metadata', e?.message || e);
                                    }
                                    // stream audio packets to client, track stats and log
                                    // prepare audio-specific abort controller so we can stop audio piping without killing other pipelines
                                    user.audioAbortController = user.audioAbortController || new AbortController();
                                    const audioSignal = user.audioAbortController.signal;

                                    // Wait for client to signal audio readiness (audio_ready). Returns true if ready within timeout.
                                    async function waitForAudioReady(user, timeoutMs = 2000) {
                                        if (!user) return false;
                                        if (user.audioReady) return true;
                                        return await new Promise((resolve) => {
                                            let resolved = false;
                                            const to = setTimeout(() => {
                                                if (!resolved) {
                                                    resolved = true;
                                                    resolve(false);
                                                }
                                            }, timeoutMs);
                                            const check = () => {
                                                if (user.audioReady && !resolved) {
                                                    resolved = true;
                                                    clearTimeout(to);
                                                    resolve(true);
                                                } else if (!resolved) setTimeout(check, 50);
                                            };
                                            check();
                                        });
                                    }

                                    // Wait briefly for client to signal audio readiness; avoids overwhelming client early
                                    try {
                                        const audioReady = await waitForAudioReady(user, 2000);
                                        if (!audioReady) {
                                            logger.debug(`Audio: client did not signal ready within timeout for id=${id}; continuing but client may drop packets`);
                                        } else {
                                            logger.debug(`Audio: client signalled ready for id=${id}`);
                                        }
                                    } catch (e) {
                                        logger.debug(`Audio readiness check failed for id=${id}: ${e?.message || e}`);
                                    }

                                    audioPacketStream.pipeTo(new WritableStream({
                                        write(packet) {
                                            try {
                                                // unwrap common wrappers to obtain a raw binary buffer
                                                // fast-path: many scrcpy/audio implementations emit { type, keyframe, pts, data }
                                                let buf = null;
                                                let pktType = typeof packet;
                                                let pktLen = null;
                                                let info = null;
                                                try {
                                                    if (packet && typeof packet === 'object' && ('data' in packet)) {
                                                        const d = packet.data;
                                                        if (d instanceof Uint8Array || (typeof Buffer !== 'undefined' && Buffer.isBuffer(d))) {
                                                            buf = d;
                                                            pktType = 'Wrapped(data:Uint8Array)';
                                                            pktLen = d.length;
                                                        } else if (typeof ArrayBuffer !== 'undefined' && ArrayBuffer.isView && ArrayBuffer.isView(d)) {
                                                            try {
                                                                buf = new Uint8Array(d.buffer, d.byteOffset || 0, d.byteLength || d.buffer.byteLength);
                                                                pktType = 'Wrapped(data:ArrayBufferView)';
                                                                pktLen = buf.length;
                                                            } catch (e) { /* ignore */
                                                            }
                                                        } else if (typeof ArrayBuffer !== 'undefined' && d instanceof ArrayBuffer) {
                                                            buf = new Uint8Array(d);
                                                            pktType = 'Wrapped(data:ArrayBuffer)';
                                                            pktLen = buf.length;
                                                        } else if (typeof d === 'string') {
                                                            // maybe base64
                                                            const s = d.trim();
                                                            if (s.length > 32 && /^[A-Za-z0-9+/=\s]+$/.test(s)) {
                                                                try {
                                                                    const b = Buffer.from(s.replace(/\s+/g, ''), 'base64');
                                                                    if (b && b.length > 0) {
                                                                        buf = b;
                                                                        pktType = 'Wrapped(data:Base64String)';
                                                                        pktLen = b.length;
                                                                    }
                                                                } catch (e) { /* ignore */
                                                                }
                                                            }
                                                        }
                                                        if (buf) {
                                                            // use buf directly
                                                        } else {
                                                            // fallback to generic extractor
                                                            const res = extractAudioBuffer(packet);
                                                            buf = res.buf;
                                                            pktType = res.pktType;
                                                            pktLen = res.pktLen;
                                                            info = res.info;
                                                        }
                                                    } else {
                                                        const res = extractAudioBuffer(packet);
                                                        buf = res.buf;
                                                        pktType = res.pktType;
                                                        pktLen = res.pktLen;
                                                        info = res.info;
                                                    }
                                                } catch (e) {
                                                    // safety: if anything crashes during quick unwrap, fallback to extractor
                                                    try {
                                                        const res = extractAudioBuffer(packet);
                                                        buf = res.buf;
                                                        pktType = res.pktType;
                                                        pktLen = res.pktLen;
                                                        info = res.info;
                                                    } catch (ee) {
                                                        buf = null;
                                                        pktType = typeof packet;
                                                        pktLen = null;
                                                    }
                                                }
                                                const sendPacket = buf || packet; // if no binary found, fallback to original
                                                const usedPktType = buf ? 'Uint8Array' : pktType;
                                                const usedPktLen = buf ? pktLen : (packet && packet.length ? packet.length : null);

                                                user.audioStats.sent += 1;
                                                user.audioStats.lastPacketSize = usedPktLen || 0;
                                                user.audioStats.lastPacketTs = Date.now();

                                                // Validate audio packet for codec-specific expectations
                                                try {
                                                    const validation = validateAudioPacket(buf, metadata || {}, id, user);
                                                    if (!validation.ok) {
                                                        logger.warn(`Audio validation failed for id=${id}: ${validation.reason}`, validation.details || {});
                                                        user.audioStats.dropped += 1;
                                                        try {
                                                            sendToUser(user, packer.pack({
                                                                media: 'audio_invalid',
                                                                reason: validation.reason,
                                                                details: validation.details
                                                            }));
                                                        } catch (e) {
                                                            logger.debug('Failed to notify user about invalid audio packet', e?.message || e);
                                                        }
                                                        // skip sending this packet
                                                        return;
                                                    } else {
                                                        //logger.debug(`Audio validation passed for id=${id}`, validation.details || {});
                                                    }
                                                } catch (e) {
                                                    logger.debug('Audio validation threw exception', e?.message || e);
                                                }

                                                // Debug info for problematic packets
                                                if (!buf || usedPktLen === 0) {
                                                    logger.warn(`Audio packet with unexpected shape/type id=${id} type=${usedPktType} pktLen=${usedPktLen} sent=${user.audioStats.sent}`);
                                                    try {
                                                        if (buf && buf.slice) {
                                                            const preview = buf.slice(0, 8);
                                                            logger.debug('Audio packet preview (hex)', Array.from(preview).map(b => b.toString(16).padStart(2, '0')).join(' '));
                                                        } else if (info && info.keys) {
                                                            logger.debug('Audio packet wrapper info keys', info.keys);
                                                            try {
                                                                logger.debug('Audio packet keys preview', JSON.stringify(Object.keys(packet).slice(0, 20)));
                                                            } catch (e) {
                                                                logger.debug('Could not stringify packet keys', e?.message || e);
                                                            }
                                                        } else {
                                                            try {
                                                                logger.debug('Audio packet content preview', JSON.stringify(packet).slice(0, 200));
                                                            } catch (e) {
                                                                logger.debug('Could not stringify audio packet content', e?.message || e);
                                                            }
                                                        }
                                                    } catch (e) {
                                                        logger.debug('Failed to preview audio packet', e?.message || e);
                                                        try {
                                                            // additional inspection to help debug: check packet.data
                                                            try {
                                                                logger.debug('packet.constructor', packet && packet.constructor ? packet.constructor.name : typeof packet);
                                                            } catch (ee) {
                                                            }
                                                            try {
                                                                logger.debug('packet has data key', packet && Object.prototype.hasOwnProperty.call(packet, 'data'), 'in operator', packet && ('data' in packet));
                                                            } catch (ee) {
                                                            }
                                                            try {
                                                                logger.debug('packet.data constructor', packet && packet.data && packet.data.constructor ? packet.data.constructor.name : typeof (packet && packet.data));
                                                            } catch (ee) {
                                                            }
                                                            try {
                                                                logger.debug('packet.data instanceof Uint8Array', packet && (packet.data instanceof Uint8Array));
                                                            } catch (ee) {
                                                            }
                                                            try {
                                                                logger.debug('packet.data is Buffer', packet && (typeof Buffer !== 'undefined' && Buffer.isBuffer(packet.data)));
                                                            } catch (ee) {
                                                            }
                                                            try {
                                                                logger.debug('packet keys', packet && typeof packet === 'object' ? Object.keys(packet) : 'n/a');
                                                            } catch (ee) {
                                                            }
                                                        } catch (eee) {
                                                            logger.debug('Extra inspection failed', eee?.message || eee);
                                                        }
                                                    }
                                                }

                                                // Send and check ws buffer/backpressure
                                                const beforeBuffered = (user.ws && typeof user.ws.getBufferedAmount === 'function') ? user.ws.getBufferedAmount() : null;
                                                const packed = packer.pack({media: 'audio', packet: sendPacket});
                                                const ok = sendToUser(user, packed);

                                                if (!ok) {
                                                    user.audioStats.dropped += 1;
                                                    logger.debug(`Audio packet dropped (sendToUser failed) id=${id} sent=${user.audioStats.sent} dropped=${user.audioStats.dropped} beforeBuffered=${beforeBuffered}`);
                                                } else {
                                                    if (user.audioStats.sent % 50 === 0) {
                                                        const afterBuffered = (user.ws && typeof user.ws.getBufferedAmount === 'function') ? user.ws.getBufferedAmount() : null;
                                                        logger.debug(`Audio packets sent id=${id} count=${user.audioStats.sent} lastSize=${user.audioStats.lastPacketSize} dropped=${user.audioStats.dropped} bufBefore=${beforeBuffered} bufAfter=${afterBuffered}`);
                                                    }
                                                }

                                                // If too many drops in a short period, notify client and log and abort audio stream to conserve resources
                                                const DROP_ABORT_THRESHOLD = 200;
                                                if (user.audioStats.dropped >= DROP_ABORT_THRESHOLD) {
                                                    logger.warn(`Audio too many drops for id=${id} (${user.audioStats.dropped}), aborting audio stream to relieve backpressure`);
                                                    try {
                                                        sendToUser(user, packer.pack({
                                                            media: 'audio_backpressure',
                                                            message: 'server aborting audio stream due to backpressure',
                                                            dropped: user.audioStats.dropped
                                                        }));
                                                    } catch (e) {
                                                        logger.debug('Failed to send audio_backpressure', e?.message || e);
                                                    }
                                                    try {
                                                        if (user.audioAbortController && !user.audioAbortController.signal.aborted) user.audioAbortController.abort();
                                                    } catch (e) {
                                                        logger.debug('Failed to abort audioAbortController', e?.message || e);
                                                    }
                                                }

                                            } catch (ex) {
                                                user.audioStats.dropped += 1;
                                                logger.error(`Error sending audio packet id=${id} sent=${user.audioStats.sent} dropped=${user.audioStats.dropped}: ${ex?.message || ex}`);
                                                try {
                                                    sendToUser(user, packer.pack({
                                                        media: 'audio_error',
                                                        message: String(ex)
                                                    }));
                                                } catch (e) { /* ignore */
                                                }
                                            }
                                        }
                                    }), {signal: audioSignal}).catch((e) => {
                                        if (audioSignal && audioSignal.aborted) {
                                            logger.info(`Audio stream aborted for id=${id}`);
                                        } else if (user?.abortController?.signal?.aborted) {
                                            logger.info(`Audio stream stopped because connection aborted for id=${id}`);
                                        } else {
                                            logger.error('audioPacketStream pipe error', e);
                                        }
                                    });
                                    break;
                                }
                                default:
                                    logger.debug('Unknown audio metadata.type', metadata && metadata.type);
                                    break;
                            }
                        } catch (e) {
                            logger.error('Exception while handling client.audioStream', e?.message || e);
                        }
                    }

                    // video
                    if (client.videoStream) {
                        const {metadata: videoMetadata, stream: videoPacketStream} = await client.videoStream;
                        logger.info(videoMetadata);
                        // store metadata on user for touch normalization
                        try {
                            user.videoMetadata = videoMetadata;
                        } catch (e) {
                            logger.debug('Could not store videoMetadata on user', e?.message || e);
                        }
                        sendToUser(user, packer.pack({media: 'video_metadata', packet: videoMetadata}));
                        videoPacketStream.pipeTo(new WritableStream({
                            write(packet) {
                                try {
                                    sendToUser(user, packer.pack({media: 'video', packet}));
                                } catch (ex) {
                                    logger.error(ex);
                                }
                            }
                        }), {signal: user.abortController?.signal || undefined}).catch((e) => {
                            if (user?.abortController?.signal?.aborted) return;
                            logger.error(e);
                        });
                    }

                } catch (err) {
                    logger.error(err);
                    try {
                        ws.send(packer.pack({media: 'error', message: 'internal server error'}), true);
                    } catch (e) {
                    }
                    if (ws.id) global.users.delete(ws.id);
                    closeSocket(ws, 'internal server error', true);
                }
            },
            message: (ws, message) => {
                const {id} = ws;
                try {
                    const user = global.users.get(id);
                    const record = unpacker.unpack(message);
                    // Log the incoming command and a short description of the payload for debugging
                    // Log incoming messages at debug level (useful for troubleshooting); respects LOG_LEVEL
                    if (logger && typeof logger.debug === 'function') {
                        let payloadDesc;
                        try {
                            const p = record?.payload;
                            if (p == null) payloadDesc = 'null';
                            else if (p instanceof Uint8Array || (typeof Buffer !== 'undefined' && p instanceof Buffer)) payloadDesc = `binary(len=${p.length})`;
                            else if (typeof p === 'string') payloadDesc = `string(len=${p.length})`;
                            else if (typeof p === 'object') payloadDesc = `object(keys=${Object.keys(p || {}).length})`;
                            else payloadDesc = String(typeof p);
                        } catch (e) {
                            payloadDesc = 'unknown';
                        }
                        logger.debug(`WS message received id=${id} cmd=${record?.cmd ?? '<none>'} payload=${payloadDesc}`);
                    }

                    // Helper to stringify payloads safely (handle Buffers/Uint8Array, BigInt, cycles)
                    const safeStringify = (obj) => {
                        const seen = new WeakSet();
                        const serialize = (value) => {
                            if (value === null) return null;
                            const t = typeof value;
                            if (t === 'bigint') return value.toString() + 'n';
                            if (t === 'function') return `[Function ${value.name || 'anonymous'}]`;
                            if (t !== 'object') return value;
                            if (value instanceof Uint8Array) return `Uint8Array(len=${value.length})`;
                            if (typeof Buffer !== 'undefined' && value instanceof Buffer) return `Buffer(len=${value.length})`;
                            if (seen.has(value)) return '[Circular]';
                            seen.add(value);
                            if (Array.isArray(value)) return value.map(serialize);
                            const out = {};
                            for (const k of Object.keys(value)) {
                                try {
                                    out[k] = serialize(value[k]);
                                } catch (e) {
                                    out[k] = `[Unserializable:${String(e)}]`;
                                }
                            }
                            return out;
                        };
                        try {
                            return JSON.stringify(serialize(obj), null, 2);
                        } catch (e) {
                            try {
                                return String(obj);
                            } catch (_) {
                                return '[Unstringiable]';
                            }
                        }
                    };

                    // Safe invoker for controller methods with extensive logging and error handling
                    const safeInvokeController = async (methodName, payload) => {
                        try {
                            if (!user) {
                                logger.error(`safeInvokeController: no user for id=${id} when calling ${methodName}`);
                                return;
                            }
                            const controller = user.client?.controller;
                            if (!controller) {
                                logger.error(`safeInvokeController: no controller available for id=${id} method=${methodName}`);
                                return;
                            }
                            if (typeof controller[methodName] !== 'function') {
                                logger.error(`safeInvokeController: controller method not found: ${methodName} for id=${id}`);
                                logger.debug(`Available controller methods: ${Object.keys(controller).filter(k => typeof controller[k] === 'function').join(', ')}`);
                                return;
                            }
                            logger.debug(`Invoking controller.${methodName} id=${id} payload=${safeStringify(payload)}`);
                            // Special handling for injectTouch: normalize payload to scrcpy expected format.
                            let callPayload = payload;
                            if (methodName === 'injectTouch') {
                                try {
                                    logger.debug(`normalizeTouchPayload call for id=${id} payload=${safeStringify(payload)}`);
                                    let normalized = await normalizeTouchPayload(user, payload);
                                    if (!normalized) {
                                        logger.debug('normalizeTouchPayload returned null, attempting fallback normalizer', {payload: safeStringify(payload)});
                                        const fb = fallbackNormalizePayload(user, payload);
                                        if (fb) {
                                            normalized = fb;
                                            logger.debug('Fallback normalizer produced a payload, using it', {payload: safeStringify(normalized)});
                                        } else {
                                            logger.error(`injectTouch: could not normalize payload (strict and fallback failed). payload=${safeStringify(payload)}`);
                                            try {
                                                ws.send(packer.pack({
                                                    media: 'error',
                                                    message: 'injectTouch: invalid payload'
                                                }), true);
                                            } catch (e) {
                                            }
                                            return;
                                        }
                                    }
                                    // at this point 'normalized' is guaranteed to be non-null
                                    callPayload = normalized;
                                    logger.debug(`injectTouch normalized payload=${safeStringify(callPayload)}`);
                                } catch (e) {
                                    const errObj = serializeError(e);
                                    logger.error(`injectTouch normalization error: ${safeStringify(errObj)}`);
                                    return;
                                }
                            }
                            // allow both sync and Promise-returning methods
                            // Defensive conversion: ensure no BigInt fields remain in callPayload (some serializers choke on BigInt)
                            try {
                                if (callPayload && typeof callPayload === 'object') {
                                    // recursively set pointerId to BigInt if present in payload or nested objects
                                    const setPointerIdRec = (o) => {
                                        if (!o || typeof o !== 'object') return;
                                        for (const [k, val] of Object.entries(o)) {
                                            if (k === 'pointerId' || k === 'pointer_id' || k === 'pid') {
                                                try {
                                                    if (typeof val === 'bigint') o[k] = val;
                                                    else if (typeof val === 'number') o[k] = BigInt(Math.trunc(val));
                                                    else if (typeof val === 'string') {
                                                        const s = val.trim().replace(/n$/i, '');
                                                        const num = Number(s);
                                                        if (!Number.isNaN(num)) o[k] = BigInt(Math.trunc(num));
                                                        else o[k] = BigInt(-2);
                                                    } else {
                                                        o[k] = BigInt(-2);
                                                    }
                                                } catch (e) {
                                                    o[k] = BigInt(-2);
                                                }
                                            } else if (val && typeof val === 'object') {
                                                setPointerIdRec(val);
                                            }
                                        }
                                    };
                                    setPointerIdRec(callPayload);
                                    // convert any other BigInt fields to Number to avoid serializer mixing issues (except pointerId)
                                    const convertNonPointerBigInt = (o) => {
                                        if (!o || typeof o !== 'object') return;
                                        for (const [k, val] of Object.entries(o)) {
                                            if (k === 'pointerId' || k === 'pointer_id' || k === 'pid') continue; // keep as BigInt
                                            if (typeof val === 'bigint') o[k] = Number(val);
                                            else if (val && typeof val === 'object') convertNonPointerBigInt(val);
                                        }
                                    };
                                    convertNonPointerBigInt(callPayload);
                                    // coerce known numeric string keys to numbers
                                    const numericKeys = ['pointerX', 'pointerY', 'videoWidth', 'videoHeight', 'pressure', 'buttons', 'action'];
                                    for (const k of numericKeys) {
                                        if (k in callPayload) {
                                            const val = callPayload[k];
                                            if (typeof val === 'string') {
                                                const s = val.trim();
                                                const num = Number(s);
                                                if (!Number.isNaN(num)) callPayload[k] = num;
                                            }
                                            if (typeof callPayload[k] === 'bigint') callPayload[k] = Number(callPayload[k]);
                                        }
                                    }
                                    // debug pointerId type/value (top-level)
                                    try {
                                        const pid = callPayload.pointerId ?? callPayload.pointer_id ?? callPayload.pid;
                                        logger.debug('Sanitized pointerId', {value: pid, type: typeof pid});
                                    } catch (e) {
                                    }
                                    logger.debug('Calling controller with sanitized payload types', Object.keys(callPayload).reduce((acc, k) => {
                                        acc[k] = typeof callPayload[k];
                                        return acc;
                                    }, {}));
                                }
                            } catch (e) {
                                logger.debug('Error sanitizing callPayload:', e);
                            }
                            const res = controller[methodName](callPayload);
                            if (res && typeof res.then === 'function') {
                                await res;
                            }
                        } catch (err) {
                            logger.error(`Error invoking controller.${methodName} for id=${id}: ${err?.message || err}`);
                            logger.debug(err?.stack || err);
                            try {
                                ws.send(packer.pack({
                                    media: 'error',
                                    message: `controller.${methodName} failed: ${err?.message || err}`
                                }), true);
                            } catch (e) { /* ignore send error */
                            }
                        }
                    };

                    // Handle volume commands even if controller is not yet available
                    {
                        const cmdAliasMapLocal = {
                            'injectPointer': 'injectTouch',
                            'injectMouse': 'injectTouch',
                            'pointer': 'injectTouch',
                            'setDeviceVolume': 'setVolume',
                            'setvolume': 'setVolume',
                            'setVolume': 'setVolume',
                        };
                        const incomingCmd = record.cmd;
                        const normalizedCmd = cmdAliasMapLocal[incomingCmd] || incomingCmd;
                        if (normalizedCmd === 'setVolume') {
                            (async () => {
                                try {
                                    logger.debug(`setVolume requested for id=${id} payload=${safeStringify(record.payload)}`);
                                    const payload = record.payload || {};
                                    // Require controller for key injection; do not use subprocess.exec per request
                                    if (!user || !user.client || !user.client.controller) {
                                        const errMsg = 'setVolume: controller not available on this connection; cannot inject key events';
                                        logger.error(errMsg);
                                        try {
                                            sendToUser(user, packer.pack({
                                                media: 'message',
                                                type: 'volume_set',
                                                payload: {ok: false, error: 'controller-not-available'}
                                            }), true);
                                        } catch (e) {
                                            logger.debug('Failed to send setVolume error', e?.message || e);
                                        }
                                        return;
                                    }

                                    // helper to press a key (down + small delay + up)
                                    const pressKey = async (keyCode) => {
                                        await safeInvokeController('injectKeyCode', {action: 0, keyCode});
                                        // short delay to emulate a real keypress
                                        await new Promise((r) => setTimeout(r, 50));
                                        await safeInvokeController('injectKeyCode', {action: 1, keyCode});
                                    };

                                    // Normalize payload like previous implementation
                                    let action = null;
                                    let targetPercent = null;
                                    let deltaSteps = null;
                                    if (typeof payload === 'string') action = payload.toLowerCase();
                                    else if (typeof payload === 'number') {
                                        const v = payload;
                                        if (v >= 0 && v <= 1) targetPercent = Math.round(v * 100);
                                        else if (v > 1 && v <= 100) targetPercent = Math.round(v);
                                        else targetPercent = Math.round(Math.max(0, Math.min(100, v)));
                                    } else if (payload && typeof payload === 'object') {
                                        if (typeof payload.delta === 'number') deltaSteps = Math.trunc(payload.delta);
                                        if (typeof payload.volume === 'number') {
                                            const v = payload.volume;
                                            if (v >= 0 && v <= 1) targetPercent = Math.round(v * 100);
                                            else if (v > 1 && v <= 100) targetPercent = Math.round(v);
                                        }
                                        if (typeof payload.action === 'string') action = payload.action.toLowerCase();
                                        if (typeof payload.muted === 'boolean') {
                                            if (payload.muted) action = 'mute';
                                            else {
                                                // unmute via volume up once
                                                action = 'up';
                                            }
                                        }
                                    }

                                    // Special-case: if the caller passed an exact 0 or 1 volume (either as number or in payload.volume),
                                    // treat that as a small single-step change instead of setting to absolute min/max.
                                    try {
                                        const volVal = (typeof payload === 'number') ? payload : (payload && typeof payload === 'object' && typeof payload.volume === 'number' ? payload.volume : null);
                                        if (volVal !== null && (Number(volVal) === 0 || Number(volVal) === 1)) {
                                            if (!user || !user.client || !user.client.controller) {
                                                try {
                                                    sendToUser(user, packer.pack({
                                                        media: 'message',
                                                        type: 'volume_set',
                                                        payload: {ok: false, error: 'controller-not-available'}
                                                    }));
                                                } catch (e) {
                                                }
                                                return;
                                            }
                                            if (Number(volVal) === 0) {
                                                // small decrease
                                                await pressKey(25);
                                                try {
                                                    sendToUser(user, packer.pack({
                                                        media: 'message',
                                                        type: 'volume_set',
                                                        payload: {ok: true, action: 'small-decrease'}
                                                    }));
                                                } catch (e) {
                                                    logger.debug('Failed to send setVolume response', e?.message || e);
                                                }
                                                return;
                                            } else {
                                                // small increase
                                                await pressKey(24);
                                                try {
                                                    sendToUser(user, packer.pack({
                                                        media: 'message',
                                                        type: 'volume_set',
                                                        payload: {ok: true, action: 'small-increase'}
                                                    }));
                                                } catch (e) {
                                                    logger.debug('Failed to send setVolume response', e?.message || e);
                                                }
                                                return;
                                            }
                                        }
                                    } catch (e) {
                                        logger.debug('setVolume small-step handler error', e?.message || e);
                                    }

                                    // Map to Android key codes: VolumeUp=24, VolumeDown=25, Mute=164
                                    if (action === 'up') {
                                        await pressKey(24);
                                        try {
                                            sendToUser(user, packer.pack({
                                                media: 'message',
                                                type: 'volume_set',
                                                payload: {ok: true, action: 'up'}
                                            }));
                                        } catch (e) {
                                            logger.debug('Failed to send setVolume response', e?.message || e);
                                        }
                                        return;
                                    }
                                    if (action === 'down') {
                                        await pressKey(25);
                                        try {
                                            sendToUser(user, packer.pack({
                                                media: 'message',
                                                type: 'volume_set',
                                                payload: {ok: true, action: 'down'}
                                            }));
                                        } catch (e) {
                                            logger.debug('Failed to send setVolume response', e?.message || e);
                                        }
                                        return;
                                    }
                                    if (action === 'mute' || action === 'toggle-mute') {
                                        await pressKey(164);
                                        try {
                                            sendToUser(user, packer.pack({
                                                media: 'message',
                                                type: 'volume_set',
                                                payload: {ok: true, action: 'mute'}
                                            }));
                                        } catch (e) {
                                            logger.debug('Failed to send setVolume response', e?.message || e);
                                        }
                                        return;
                                    }

                                    if (typeof deltaSteps === 'number') {
                                        const stepKey = deltaSteps > 0 ? 24 : 25;
                                        const steps = Math.min(Math.abs(deltaSteps), 200);
                                        for (let i = 0; i < steps; i++) {
                                            try {
                                                await pressKey(stepKey);
                                            } catch (e) {
                                                logger.error('setVolume delta pressKey failed', e?.message || e);
                                            }
                                            await new Promise((r) => setTimeout(r, 80));
                                        }
                                        try {
                                            sendToUser(user, packer.pack({
                                                media: 'message',
                                                type: 'volume_set',
                                                payload: {ok: true, delta: deltaSteps}
                                            }));
                                        } catch (e) {
                                            logger.debug('Failed to send setVolume response', e?.message || e);
                                        }
                                        return;
                                    }

                                    if (typeof targetPercent === 'number') {
                                        // best-effort: clamp to min (many downs) then ups to desired index
                                        const assumedMax = 15;
                                        const desiredIndex = Math.round((targetPercent / 100) * assumedMax);
                                        const clampDowns = Math.min(assumedMax + 5, 40);
                                        for (let i = 0; i < clampDowns; i++) {
                                            try {
                                                await pressKey(25);
                                            } catch (e) {
                                                logger.error('setDeviceVolume clamp down pressKey failed', e?.message || e);
                                            }
                                            await new Promise((r) => setTimeout(r, 60));
                                        }
                                        const ups = Math.min(desiredIndex, 60);
                                        for (let i = 0; i < ups; i++) {
                                            try {
                                                await pressKey(24);
                                            } catch (e) {
                                                logger.error('setDeviceVolume volume up pressKey failed', e?.message || e);
                                            }
                                            await new Promise((r) => setTimeout(r, 80));
                                        }
                                        if (payload && payload.muted) {
                                            try {
                                                await pressKey(164);
                                            } catch (e) {
                                                logger.error('setDeviceVolume mute pressKey failed', e?.message || e);
                                            }
                                        }
                                        try {
                                            sendToUser(user, packer.pack({
                                                media: 'message',
                                                type: 'volume_set',
                                                payload: {ok: true, note: 'best-effort-applied', targetPercent}
                                            }));
                                        } catch (e) {
                                            logger.debug('Failed to send setVolume response', e?.message || e);
                                        }
                                        return;
                                    }

                                    // If nothing matched
                                    try {
                                        sendToUser(user, packer.pack({
                                            media: 'message',
                                            type: 'volume_set',
                                            payload: {ok: false, error: 'invalid-payload'}
                                        }));
                                    } catch (e) {
                                        logger.debug('Failed to send setVolume invalid payload', e?.message || e);
                                    }
                                    return;
                                } catch (e) {
                                    logger.error('setVolume handler error', e?.message || e);
                                    try {
                                        sendToUser(user, packer.pack({
                                            media: 'message',
                                            type: 'volume_set',
                                            payload: {error: String(e)}
                                        }), true);
                                    } catch (er) {
                                        logger.debug('Failed to send setVolume exception', er?.message || er);
                                    }
                                }
                            })();
                            return;
                        }
                    }

                    if (user?.client?.controller) {
                        // Route common control commands through the safe invoker
                        const cmdAliasMap = {
                            'injectPointer': 'injectTouch',
                            'injectMouse': 'injectTouch',
                            'pointer': 'injectTouch',
                        };
                        const incomingCmd = record.cmd;
                        const normalizedCmd = cmdAliasMap[incomingCmd] || incomingCmd;

                        if (normalizedCmd === 'injectKeyCode') {
                            safeInvokeController('injectKeyCode', record.payload);
                        } else if (normalizedCmd === 'injectTouch') {
                            // support array of touch events
                            if (Array.isArray(record.payload)) {
                                for (const ev of record.payload) {
                                    safeInvokeController('injectTouch', ev);
                                }
                            } else {
                                safeInvokeController('injectTouch', record.payload);
                            }
                        } else if (normalizedCmd === 'injectScroll') {
                            // Detailed debug for injectScroll: record what the client sent and capture any controller errors
                            try {
                                logger.debug(`injectScroll received id=${id} payload=${safeStringify(record.payload)}`);
                            } catch (e) {
                                logger.debug('injectScroll payload stringify failed', e?.message || e);
                            }
                            // Normalize scroll payload before invoking controller
                            try {
                                const normalized = normalizeScrollPayload(user, record.payload);
                                if (normalized) {
                                    // sanitize pointerId for controller: convert BigInt to Number if necessary
                                    try {
                                        if (normalized.pointerId && typeof normalized.pointerId === 'bigint') {
                                            // convert to Number safely (may lose range but pointer ids are small)
                                            normalized.pointerId = Number(normalized.pointerId);
                                        }
                                    } catch (e) {
                                        logger.debug('injectScroll pointerId conversion failed', e?.message || e);
                                    }
                                    logger.debug(`injectScroll normalized payload for id=${id}: ${safeStringify(normalized)}`);
                                    safeInvokeController('injectScroll', normalized);
                                } else {
                                    logger.debug(`injectScroll: normalization returned null for id=${id}, sending raw payload to controller`);
                                    safeInvokeController('injectScroll', record.payload);
                                }
                            } catch (err) {
                                logger.error(`injectScroll invocation failed for id=${id}: ${err?.message || err}`);
                                logger.debug(err?.stack || err);
                                try {
                                    ws.send(packer.pack({
                                        media: 'error',
                                        message: `injectScroll invocation failed: ${err?.message || String(err)}`
                                    }), true);
                                } catch (e) {
                                    logger.debug('Failed to send injectScroll invocation error to client', e?.message || e);
                                }
                            }
                        } else if (normalizedCmd === 'setScreenPowerMode') {
                            safeInvokeController('setScreenPowerMode', record.payload);
                        } else if (normalizedCmd === 'rotateDevice') {
                            safeInvokeController('rotateDevice', record.payload);
                        } else if (normalizedCmd === 'clipboardPaste') {
                            safeInvokeController('setClipboard', record.payload);
                        }
                    } else {
                        // No controller yet: log for debugging
                        logger.debug(`No client controller for id=${id}. record.cmd=${record?.cmd} payload=${safeStringify(record?.payload)}`);
                    }
                } catch (ex) {
                    logger.error(ex);
                }
            },
            drain: (ws) => {
                logger.info(`WebSocket backpressure: ${ws.getBufferedAmount()}`);
            },
            // close: async (ws) => {
            close: async (ws, code, message) => {
                logger.info(`WebSocket close event received code=${code} message=${message}`);
                try {
                    const {id} = ws;
                    const user = global.users.get(id);
                    // Log whether the server initiated the close and why (fields set by closeSocket)
                    if (user && (user._serverInitiatedClose || user._serverCloseReason)) {
                        logger.info(`WebSocket close (server-initiated) id=${id} reason=${user._serverCloseReason || '<none>'}`);
                        if (user._serverCloseStack) logger.debug(`Server close stack: ${user._serverCloseStack}`);
                    } else {
                        logger.info(`WebSocket close (client-initiated or unknown) id=${id}`);
                    }

                    if (user) {
                        // Abort any active pipelines, but don't call abort() if already aborted
                        if (user.abortController) {
                            try {
                                if (!user.abortController.signal.aborted) {
                                    user.abortController.abort();
                                }
                            } catch (err) {
                                logger.debug('AbortController.abort() threw (ignored):', err?.message || err);
                            }
                            user.abortController = undefined;
                        }
                        user.ws = null;
                        global.users.delete(id);
                        if (user.client) {
                            try {
                                await user.client.close();
                            } catch (err) {
                                logger.debug('user.client.close() error (ignored):', err?.message || err);
                            }
                        }
                    }
                } catch (ex) {
                    logger.error(ex);
                }
            }
        })
        .get('/*', (res, req) => {
            if (!requireAuthForHttp(res, req)) return;
            res.cork(() => {
                res.writeHeader('Access-Control-Allow-Origin', req.getHeader('origin') || '*');
                res.writeHeader('Access-Control-Allow-Credentials', 'true');
                res.writeHeader('Access-Control-Allow-Headers', 'Origin, X-Api-Key, X-Requested-With, Content-Type, Accept, Authorization');
                res.writeHeader('Access-Control-Allow-Methods', 'GET,HEAD,POST,PUT,PATCH,DELETE,OPTIONS');
                res.writeHeader('Content-Type', 'text/plain; charset=utf-8');
                res.writeStatus('200 OK');
                res.end('ADB streaming service is running.');
            });
        })
        .get('/_debug/audio_stats', (res, req) => {
            if (!requireAuthForHttp(res, req)) return;
            try {
                const stats = {};
                for (const [id, u] of global.users.entries()) {
                    stats[id] = {
                        audioStats: u.audioStats || {sent: 0, dropped: 0, lastPacketSize: 0, lastPacketTs: null},
                        connected: !!(u && u.client),
                        device: u?.client?.device || null,
                    };
                }
                res.writeStatus('200 OK');
                res.writeHeader('Content-Type', 'application/json; charset=utf-8');
                res.end(JSON.stringify(stats));
            } catch (e) {
                logger.error('Error in /_debug/audio_stats', e?.message || e);
                res.writeStatus('500 Internal Server Error');
                res.end('error');
            }
        })

    await app.start();
};

// Normalize touch payload into ScrcpyInjectTouchControlMessage shape
async function normalizeTouchPayload(user, payload) {
    try {
        if (!payload || typeof payload !== 'object') return null;
        // New client fields: deviceX/deviceY are absolute device pixel coords
        // displayWidth/displayHeight are the device display size
        const deviceX = payload.deviceX ?? payload.device_x ?? null;
        const deviceY = payload.deviceY ?? payload.device_y ?? null;
        const rotation = (typeof payload.rotation !== 'undefined') ? Number(payload.rotation) : (typeof payload.rot !== 'undefined' ? Number(payload.rot) : null);

        // accept many possible names for general coords
        let x = deviceX ?? payload.x ?? payload.clientX ?? payload.cx ?? payload.posX ?? payload.pointerX ?? payload.pointer_x ?? payload.screenX ?? payload.screen_x;
        let y = deviceY ?? payload.y ?? payload.clientY ?? payload.cy ?? payload.posY ?? payload.pointerY ?? payload.pointer_y ?? payload.screenY ?? payload.screen_y;

        let action = payload.action ?? payload.type ?? payload.event ?? payload.actionButton ?? payload.actionType ?? null;
        let pointerId = payload.pointerId ?? payload.pointer_id ?? payload.pid ?? payload.pointer ?? null;
        let pressure = payload.pressure ?? payload.p ?? payload.force ?? 0.5;
        let buttons = payload.buttons ?? payload.button ?? 1;

        // prefer explicit display size fields
        let videoWidth = payload.screenWidth ?? payload.screen_width ?? payload.displayWidth ?? payload.display_width ?? payload.deviceWidth ?? payload.device_width ?? payload.displayW ?? payload.width ?? payload.w ?? payload.videoWidth ?? payload.video_width ?? null;
        let videoHeight = payload.screenHeight ?? payload.screen_height ?? payload.displayHeight ?? payload.display_height ?? payload.deviceHeight ?? payload.device_height ?? payload.displayH ?? payload.height ?? payload.h ?? payload.videoHeight ?? payload.video_height ?? null;

        // normalize action if string
        if (typeof action === 'string') {
            const a = action.toLowerCase();
            if (a.includes('down')) action = 0;
            else if (a.includes('up')) action = 1;
            else if (a.includes('move')) action = 2;
            else action = Number(action) || 0;
        } else {
            action = Number(action) || 0;
        }

        // pointerId handling: accept BigInt, number, or string
        if (pointerId === null || typeof pointerId === 'undefined') {
            if (payload && typeof payload.source === 'string' && payload.source.toLowerCase().includes('mouse')) {
                pointerId = -1n;
            } else {
                pointerId = -2n;
            }
        } else {
            try {
                if (typeof pointerId === 'bigint') {
                    // ok
                } else if (typeof pointerId === 'number') {
                    pointerId = BigInt(Math.trunc(pointerId));
                } else if (typeof pointerId === 'string') {
                    const s = pointerId.trim().replace(/n$/i, '');
                    const num = Number(s);
                    if (!Number.isNaN(num)) pointerId = BigInt(Math.trunc(num));
                    else pointerId = BigInt(-2);
                } else {
                    pointerId = BigInt(-2);
                }
            } catch (e) {
                pointerId = BigInt(-2);
            }
        }

        pressure = Number(pressure) || 0.5;
        buttons = Number(buttons) || 1;

        // parse coordinates early
        x = (typeof x === 'string') ? Number(x) : x;
        y = (typeof y === 'string') ? Number(y) : y;
        x = Number(x);
        y = Number(y);
        if (!isFinite(x)) x = NaN;
        if (!isFinite(y)) y = NaN;

        // If video size not provided or zero, try to obtain it from stored metadata or client
        if ((!videoWidth || !videoHeight) || Number(videoWidth) === 0 || Number(videoHeight) === 0) {
            try {
                if (user && user.videoMetadata) {
                    const vs = user.videoMetadata;
                    videoWidth = videoWidth || vs.width || vs.codec_width || vs.display_width || vs.displayWidth || null;
                    videoHeight = videoHeight || vs.height || vs.codec_height || vs.display_height || vs.displayHeight || null;
                } else if (user && user.client && user.client.videoStream) {
                    const vsObj = await user.client.videoStream;
                    if (vsObj && vsObj.metadata) {
                        videoWidth = videoWidth || vsObj.metadata.width || vsObj.metadata.codec_width || null;
                        videoHeight = videoHeight || vsObj.metadata.height || vsObj.metadata.codec_height || null;
                    }
                }
            } catch (e) {
                // ignore
            }
        }

        // Debug: log key values that determine normalization
        try {
            logger.debug('normalizeTouchPayload debug', {
                deviceX: deviceX,
                deviceY: deviceY,
                x: x,
                y: y,
                videoWidth: videoWidth,
                videoHeight: videoHeight,
                pointerId: pointerId,
                coordsAreNormGuess: (isFinite(x) && isFinite(y) && Math.abs(x) <= 1 && Math.abs(y) <= 1 && deviceX == null && deviceY == null),
            });
        } catch (e) { /* ignore logging errors */
        }

        // Determine whether coords are normalized (0..1) or absolute (>1). If deviceX/deviceY were provided we treated them as absolute above.
        const coordsAreNormalized = (isFinite(x) && isFinite(y) && Math.abs(x) <= 1 && Math.abs(y) <= 1 && deviceX == null && deviceY == null);

        if (coordsAreNormalized) {
            // if normalized coords, require a valid video/display size (except special-case 0,0)
            if (!isFinite(Number(videoWidth)) || !isFinite(Number(videoHeight)) || Number(videoWidth) <= 0 || Number(videoHeight) <= 0) {
                if ((Number(x) === 0 && Number(y) === 0)) {
                    videoWidth = 1;
                    videoHeight = 1;
                    logger.debug('normalizeTouchPayload: falling back to 1x1 video size for 0,0 normalized coords');
                } else {
                    logger.debug('normalizeTouchPayload: missing or invalid videoWidth/videoHeight for normalized coords', {
                        provided: {
                            videoWidth,
                            videoHeight
                        }, userHasMeta: !!(user && user.videoMetadata)
                    });
                    logger.debug('normalizeTouchPayload returning null: normalized coords but missing video size');
                    return null;
                }
            }
        } else {
            // coords look absolute. If sizes are missing or zero, fall back to 1 to satisfy serializer
            if (!isFinite(Number(videoWidth)) || !isFinite(Number(videoHeight)) || Number(videoWidth) <= 0 || Number(videoHeight) <= 0) {
                videoWidth = 1;
                videoHeight = 1;
            }
        }

        videoWidth = Number(videoWidth);
        videoHeight = Number(videoHeight);

        // If coords are normalized convert to absolute using display size
        if (coordsAreNormalized) {
            x = Math.round(x * videoWidth);
            y = Math.round(y * videoHeight);
        } else {
            if (!isFinite(x)) x = 0;
            if (!isFinite(y)) y = 0;
            x = Math.round(x);
            y = Math.round(y);
        }

        // If rotation is provided and the client coordinates were not already in device space
        // (i.e. they came from clientX/clientY or normalized), optionally rotate them into device space.
        // Rotation mapping assumes rotation is clockwise in steps of 90 degrees.
        if (rotation != null && deviceX == null && deviceY == null) {
            const rot = Number(rotation) % 4;
            if (rot !== 0) {
                let nx = x;
                let ny = y;
                if (rot === 1) { // 90° cw
                    nx = y;
                    ny = videoWidth - x - 1;
                } else if (rot === 2) { // 180°
                    nx = videoWidth - x - 1;
                    ny = videoHeight - y - 1;
                } else if (rot === 3) { // 270° cw
                    nx = videoHeight - y - 1;
                    ny = x;
                }
                // after rotation swap width/height for the serializer if needed
                if (rot === 1 || rot === 3) {
                    const tmp = videoWidth;
                    videoWidth = videoHeight;
                    videoHeight = tmp;
                }
                x = Math.round(nx);
                y = Math.round(ny);
                logger.debug('normalizeTouchPayload: applied rotation transform', {
                    rotation: rot,
                    x,
                    y,
                    videoWidth,
                    videoHeight
                });
            }
        }

        try {
            logger.debug('normalizeTouchPayload: returning normalized object', {
                action: Number(action),
                pointerId: (typeof pointerId === 'bigint') ? String(pointerId) + 'n' : String(pointerId),
                pointerIdType: typeof pointerId,
                pointerX: Number(x),
                pointerY: Number(y),
                videoWidth: Number(videoWidth),
                videoHeight: Number(videoHeight),
                pressure: Number(pressure),
                buttons: Number(buttons)
            });
        } catch (e) { /* ignore logging errors */
        }
        return {
            action: Number(action),
            pointerId: pointerId, // BigInt
            pointerX: Number(x),
            pointerY: Number(y),
            videoWidth: Number(videoWidth),
            videoHeight: Number(videoHeight),
            pressure: Number(pressure),
            buttons: Number(buttons),
        };
    } catch (err) {
        logger.error('normalizeTouchPayload unexpected error', err);
        return null;
    }
}

// Last-resort fallback normalizer: try best-effort conversion when strict normalization fails
function fallbackNormalizePayload(user, payload) {
    try {
        if (!payload || typeof payload !== 'object') return null;
        // prefer explicit device coords
        let x = payload.deviceX ?? payload.device_x ?? payload.deviceX ?? payload.pointerX ?? payload.pointer_x ?? payload.pointerX ?? payload.pointer_x ?? payload.pointerX ?? payload.x ?? payload.clientX ?? payload.cx ?? null;
        let y = payload.deviceY ?? payload.device_y ?? payload.deviceY ?? payload.pointerY ?? payload.pointer_y ?? payload.pointerY ?? payload.pointer_y ?? payload.y ?? payload.clientY ?? payload.cy ?? null;
        // try pointerX/pointerY even if they might be normalized; if normalized but display provided, scale
        let videoW = payload.screenWidth ?? payload.screen_width ?? payload.displayWidth ?? payload.display_width ?? (user && user.videoMetadata ? user.videoMetadata.width : null) ?? 1;
        let videoH = payload.screenHeight ?? payload.screen_height ?? payload.displayHeight ?? payload.display_height ?? (user && user.videoMetadata ? user.videoMetadata.height : null) ?? 1;
        x = (typeof x === 'string') ? Number(x) : x;
        y = (typeof y === 'string') ? Number(y) : y;
        if (!isFinite(x) || !isFinite(y)) return null;
        // if coords look normalized (<=1) and we have video size, scale
        if (Math.abs(x) <= 1 && Math.abs(y) <= 1 && videoW > 1 && videoH > 1) {
            x = Math.round(x * videoW);
            y = Math.round(y * videoH);
        } else {
            x = Math.round(x);
            y = Math.round(y);
        }
        let pointerId = payload.pointerId ?? payload.pointer_id ?? payload.pid ?? payload.pointer ?? -2;
        try {
            if (typeof pointerId === 'string') {
                const s = pointerId.trim().replace(/n$/i, '');
                const num = Number(s);
                pointerId = Number.isNaN(num) ? -2 : Math.trunc(num);
            } else if (typeof pointerId === 'number') {
                pointerId = Math.trunc(pointerId);
            } else if (typeof pointerId === 'bigint') {
                pointerId = Number(pointerId);
            } else {
                pointerId = -2;
            }
        } catch (e) {
            pointerId = -2;
        }
        return {
            action: Number(payload.action ?? payload.type ?? 0),
            pointerId: Number(pointerId),
            pointerX: x,
            pointerY: y,
            videoWidth: Number(videoW),
            videoHeight: Number(videoH),
            pressure: Number(payload.pressure ?? payload.p ?? 0.5) || 0.5,
            buttons: Number(payload.buttons ?? payload.button ?? 1) || 1,
        };
    } catch (err) {
        logger.debug('fallbackNormalizePayload unexpected error', err && err.message ? err.message : err);
        return null;
    }
}

// Validate incoming audio packet for common codecs and raw PCM expectations
function validateAudioPacket(buf, metadata = {}, sessionId = '<unknown>', wsObj = undefined) {
    try {
        const details = {
            sessionId,
            codec: metadata?.codec || metadata?.audioCodec || (wsObj && wsObj.audioCodec) || 'raw',
            sampleRate: metadata?.sampleRate || metadata?.sample_rate || metadata?.rate || 48000,
            channels: metadata?.channels || metadata?.channelCount || 2
        };
        if (!buf) return {ok: false, reason: 'no-binary-buffer', details};
        // buf can be Buffer or Uint8Array
        const len = (typeof buf.length === 'number') ? buf.length : null;
        details.length = len;
        if (!len || len === 0) return {ok: false, reason: 'empty-buffer', details};
        // codec-specific checks
        const codec = String(details.codec || 'raw').toLowerCase();
        if (codec === 'raw' || codec === 'pcm' || codec === 'pcm16' || codec === 'rawpcm') {
            // Expect interleaved Int16LE frames: bytes per frame = channels * 2
            const frameBytes = (Number(details.channels) || 1) * 2;
            if (frameBytes <= 0) return {ok: false, reason: 'invalid-channels', details};
            details.frameBytes = frameBytes;
            if (len % frameBytes !== 0) {
                details.remainder = len % frameBytes;
                return {ok: false, reason: 'not-multiple-of-frame', details};
            }
            // Quick sanity: check not all zeros
            let nonZero = false;
            const view = (buf instanceof Uint8Array) ? buf : Buffer.from(buf);
            for (let i = 0; i < Math.min(64, view.length); i++) {
                if (view[i] !== 0) {
                    nonZero = true;
                    break;
                }
            }
            if (!nonZero) {
                details.preview = Array.from(view.slice(0, 16));
                return {ok: false, reason: 'all-zero-preview', details};
            }
            return {ok: true, details};
        }
        // AAC ADTS check: syncword 0xFFF at start (12 bits set)
        if (codec.includes('aac')) {
            const view = (buf instanceof Uint8Array) ? buf : Buffer.from(buf);
            if (view.length >= 2) {
                const sync = ((view[0] & 0xFF) << 4) | ((view[1] & 0xF0) >> 4);
                if ((sync & 0xFFF) === 0xFFF) return {ok: true, details: Object.assign(details, {adts: true})};
                return {ok: false, reason: 'aac-no-adts-syncword', details};
            }
            return {ok: false, reason: 'aac-too-short', details};
        }
        // Opus (RTP payload / Ogg) heuristic: Opus packet often starts with 0x4f 0x70 0x75 0x73 ('Opus') in Ogg, or does not have a fixed header in RTP.
        if (codec.includes('opus')) {
            const view = (buf instanceof Uint8Array) ? buf : Buffer.from(buf);
            if (view.length >= 4) {
                // check for 'Opus' in Ogg header
                if (view[0] === 0x4f && view[1] === 0x70 && view[2] === 0x75 && view[3] === 0x73) return {
                    ok: true,
                    details: Object.assign(details, {ogg: true})
                };
                // otherwise accept as encoded packet but warn about length
                if (view.length < 10) return {ok: false, reason: 'opus-too-short', details};
                return {ok: true, details};
            }
            return {ok: false, reason: 'opus-too-short', details};
        }
        // Unknown codec: accept but warn
        return {ok: true, details: Object.assign(details, {note: 'unknown-codec-accepted'})};
    } catch (e) {
        return {ok: false, reason: 'validation-exception', error: String(e), details: {sessionId}};
    }
}

// Start the server and install global error handlers
run().then(() => {
    try {
        logger.info('ADB streaming service startup complete');
    } catch (e) {
        console.log('ADB streaming service startup complete');
    }
}).catch((err) => {
    try {
        logger.error('Failed to start ADB streaming service:', err && err.message ? err.message : err);
    } catch (e) {
        console.error('Failed to start ADB streaming service:', err);
    }
    // if startup fails, exit with non-zero code so process managers can restart
    process.exit(1);
});

// Global error handlers to capture unhandled failures and log them via the configured logger
process.on('unhandledRejection', (reason, promise) => {
    try {
        logger.error('Unhandled Rejection at:', serializeError(reason));
    } catch (e) {
        console.error('Unhandled Rejection at:', reason);
    }
});
process.on('uncaughtException', (err) => {
    try {
        logger.error('Uncaught Exception:', serializeError(err));
    } catch (e) {
        console.error('Uncaught Exception:', err);
    }
    // Exit to avoid the process continuing in an unknown state
    process.exit(1);
});
