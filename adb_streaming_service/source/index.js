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
import {serializeError} from "./utils/error.js";
import {withTimeout} from "./utils/timeout.js";
import {
    isAuthEnabled,
    validateBasicAuthHeader,
    getAuthHeaderFromRequest,
    requireAuthForHttp,
    requireAuthForUpgrade,
    httpAuthMiddleware,
} from "./utils/auth.js";
import {normalizeTouchPayload, fallbackNormalizePayload} from "./utils/normalizers/touch.js";
import {normalizeScrollPayload} from "./utils/normalizers/scroll.js";
import {validateAudioPacket, extractAudioBuffer} from "./utils/normalizers/audio.js";
import {setupAudioStream} from "./streaming/audio.js";
import {setupVideoStream} from "./streaming/video.js";
import {createUpgradeHandler} from "./websocket/upgrade.js";
import {createOpenHandler} from "./websocket/open.js";
import {createMessageHandler} from "./websocket/message.js";
import {createCloseHandler} from "./websocket/close.js";
import {audioStatsRoute} from "./routes/debug/audio-stats.js";

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
let evictPushedSerial = () => {}; // will be replaced after import
try {
    const loggerModule = await import("./services/logger.js");
    logger = loggerModule.logger;
    logger.info(`Loaded env file: ${envFile}`);
    logger.info(`Env values: SSL=${process.env.SSL} AUTH_ENABLED=${process.env.AUTH_ENABLED} PORT=${process.env.PORT} HOST=${process.env.HOST}`);
    const adbModule = await import("./services/adb/adb-tcp-service.js");
    adbTcpService = adbModule.service;
    if (typeof adbModule.evictPushedSerial === 'function') {
        evictPushedSerial = adbModule.evictPushedSerial;
    }
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

// Helper to send packed bytes to a user's websocket safely.
// Accepts user object (with .ws) and packed (Uint8Array/Buffer/ArrayBuffer).
function sendToUser(user, packed, allowCloseOnError = false) {
    try {
        if (!user || !user.ws) return false;
        const ws = user.ws;
        // Normalize packed to Uint8Array
        let data = packed;
        if (typeof Buffer !== 'undefined' && Buffer.isBuffer(packed)) {
            data = new Uint8Array(packed);
        } else if (packed instanceof ArrayBuffer) {
            data = new Uint8Array(packed);
        } else if (packed && typeof packed === 'object' && (packed.buffer instanceof ArrayBuffer) && typeof packed.byteLength === 'number') {
            // TypedArray (Uint8Array etc.)
            data = packed instanceof Uint8Array ? packed : new Uint8Array(packed.buffer, packed.byteOffset || 0, packed.byteLength || packed.buffer.byteLength);
        }

        // Best-effort check for backpressure
        let before = null;
        try {
            if (typeof ws.getBufferedAmount === 'function') before = ws.getBufferedAmount();
        } catch (e) { before = null; }

        // Send as binary
        // uWebSockets.js expects either string or ArrayBuffer/TypedArray; passing Uint8Array is fine
        ws.send(data, true);

        let after = null;
        try {
            if (typeof ws.getBufferedAmount === 'function') after = ws.getBufferedAmount();
        } catch (e) { after = null; }

        // If buffer grew a lot, log debug message
        try {
            if (before !== null && after !== null && after - before > 1024 * 64) {
                logger.debug(`sendToUser: websocket buffer grew by ${after - before} bytes for user=${user?.ws?.id || '<unknown>'}`);
            }
        } catch (e) {
            // ignore logging errors
        }

        return true;
    } catch (err) {
        try {
            logger.error('sendToUser error', err?.message || err);
            // Optionally notify client or mark user for close
            if (allowCloseOnError && user && user.ws) {
                try { user._serverInitiatedClose = true; user._serverCloseReason = 'send-error'; user._serverCloseStack = (new Error()).stack; user.ws.close(); } catch (e) {}
            }
        } catch (e) {
            // fallback console
            try { console.error('sendToUser fallback error', e); } catch (_) {}
        }
        return false;
    }
}

const run = async () => {
    logger.info(`Starting ADB Streaming Service on ${host}:${port} SSL=${sslEnabled} AUTH=${isAuthEnabled()}`);
    let app;
    try {
        app = new App({
            host,
            port,
            protocol: sslEnabled ? 'https' : 'http',
            sslOptions,
        });
    }catch (err){
        logger.error("Failed to start server:", err);
        process.exit(1);
    }

    logger.info("Configuring CORS middleware...");
    app.use(cors);
    // Enforce authentication for all HTTP routes except health endpoints
    logger.info("Configuring HTTP auth middleware...");
    app.use(httpAuthMiddleware);
    logger.info("Registering routes...");

    for (const route of routes) {
        logger.info("Registering route:", route.method, route.url);
        app.route(route, {});
    }

    // WebSocket endpoint and HTTP status
    // Build shared dependencies for all WebSocket handlers
    const wsDeps = {
        packer, unpacker, logger, users: global.users, adbTcpService,
        sendToUser, closeSocket,
        serializeError, withTimeout,
        normalizeTouchPayload, fallbackNormalizePayload, normalizeScrollPayload,
        validateAudioPacket, extractAudioBuffer,
        setupAudioStream, setupVideoStream,
    };
    const { close, drain } = createCloseHandler({ logger, users: global.users, evictPushedSerial });

    app.server
        .ws('/*', {
            compression: uWs.SHARED_COMPRESSOR,
            maxPayloadLength: 16 * 1024,
            idleTimeout: 0,
            upgrade: createUpgradeHandler({ requireAuthForUpgrade }),
            open: createOpenHandler(wsDeps),
            message: createMessageHandler(wsDeps),
            drain,
            close,
        })
        .get('/_debug/audio_stats', audioStatsRoute)
        .get('/*', (res, req) => {
            logger.info("HTTP %s %s", req.getMethod(), req.getUrl());
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

    await app.start();
};

// Start the server and install global error handlers
run().then(() => {
    logger.info('ADB streaming service startup complete');
}).catch((err) => {
    try {
        logger.error('Failed to start ADB streaming service:', serializeError(err));
    } catch (logErr) {
        // Ensure something is printed even if logger misbehaves
        try { console.error('Failed to start ADB streaming service:', err && err.stack ? err.stack : err); } catch (e) {}
    }
    process.exit(1);
});

// Global error handlers to capture unhandled failures and log them via the configured logger
process.on('unhandledRejection', (reason, promise) => {
    try {
        logger.error(`Unhandled Rejection at: ${JSON.stringify(serializeError(reason))}`);
    } catch (e) {
        console.error('Unhandled Rejection at:', reason);
    }
});
process.on('uncaughtException', (err) => {
    try {
        logger.error(`Uncaught Exception: ${JSON.stringify(serializeError(err))}`);
    } catch (e) {
        console.error('Uncaught Exception:', err);
    }
    // Exit to avoid the process continuing in an unknown state
    process.exit(1);
});

// Helper to close a websocket connection from the server side with diagnostics
function closeSocket(ws, reason = 'server-initiated', serverInitiated = true) {
    try {
        if (!ws) return;
        // If the ws has an id and a user object, prefer that for cleanup
        const id = ws.id;
        const user = id ? global.users.get(id) : null;

        // Attach metadata either on the user object or directly on the ws so the close handler can log it
        const stack = (new Error()).stack;
        try {
            if (user) {
                user._serverInitiatedClose = !!serverInitiated;
                user._serverCloseReason = reason || 'server-initiated';
                user._serverCloseStack = stack;
            } else {
                // fallback: attach to ws so the close handler (which may look at user) still can inspect
                try { ws._serverInitiatedClose = !!serverInitiated; } catch (e) {}
                try { ws._serverCloseReason = reason || 'server-initiated'; } catch (e) {}
                try { ws._serverCloseStack = stack; } catch (e) {}
            }
        } catch (e) {
            // ignore metadata attach failures
        }

        // If we have a user object, try to abort ongoing pipelines and close the adb client
        if (user) {
            try {
                if (user.abortController && !user.abortController.signal.aborted) {
                    try { user.abortController.abort(); } catch (e) { logger.debug('abortController.abort() failed during closeSocket', e?.message || e); }
                }
            } catch (e) {
                logger.debug('Error aborting user pipelines during closeSocket', e?.message || e);
            }
            try {
                if (user.client && typeof user.client.close === 'function') {
                    user.client.close().catch ? user.client.close().catch((e) => logger.debug('user.client.close() rejected during closeSocket', e?.message || e)) : null;
                }
            } catch (e) {
                logger.debug('Error closing user.client during closeSocket', e?.message || e);
            }

            // Remove user from global map
            try {
                user.ws = null;
                global.users.delete(id);
            } catch (e) {
                logger.debug('Failed to remove user from global.users during closeSocket', e?.message || e);
            }
        }

        // Finally close the websocket itself (best-effort)
        try {
            if (typeof ws.close === 'function') {
                // uWebSockets.js close() accepts optional code/message; we call without to let lib pick defaults
                ws.close();
            } else if (typeof ws.end === 'function') {
                ws.end();
            }
        } catch (e) {
            try { logger.debug('ws.close()/end() threw during closeSocket (ignored)', e?.message || e); } catch (_) {}
        }
    } catch (err) {
        try { logger.error('closeSocket error', err?.message || err); } catch (_) {}
    }
}


