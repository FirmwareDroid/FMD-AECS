import fs from "node:fs/promises";
import { BIN, VERSION } from "@yume-chan/fetch-scrcpy-server";
import { Adb, ADB_SYNC_MAX_PACKET_SIZE } from "@yume-chan/adb";
import {
	ReadableStream,
	Consumable,
	InspectStream,
	DistributionStream,
} from "@yume-chan/stream-extra";
import { AdbServerNodeTcpConnector } from "@yume-chan/adb-server-node-tcp";
import { AdbServerClient } from "@yume-chan/adb";
import {
	ScrcpyInstanceId,
	DefaultServerPath,
} from "@yume-chan/scrcpy";

import {
	AdbScrcpyClient,
	AdbScrcpyOptionsLatest,
	AdbScrcpyOptions2_1,
} from "@yume-chan/adb-scrcpy";
import { logger } from "../logger.js";
import { global } from "../../state/global.js";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
// inline __dirname when needed to avoid keeping an unused constant

export class ProgressStream extends InspectStream {
	constructor(onProgress) {
		super((chunk) => {
			onProgress((chunk && chunk.value && chunk.value.byteLength) || 0);
		});
	}
}

const DEVICE_SERVER_PATH = DefaultServerPath;
// Allow override of local server binary via DEFAULT_SERVER_PATH env (resolved relative to this module)
const userProvidedLocalServerPath = typeof process.env.DEFAULT_SERVER_PATH !== 'undefined';
let LOCAL_SERVER_PATH_RESOLVED = null;
if (userProvidedLocalServerPath) {
	const DEFAULT_SERVER_PATH = process.env.DEFAULT_SERVER_PATH;
	try {
		LOCAL_SERVER_PATH_RESOLVED = resolve(dirname(fileURLToPath(import.meta.url)), DEFAULT_SERVER_PATH);
	} catch (e) {
		LOCAL_SERVER_PATH_RESOLVED = DEFAULT_SERVER_PATH;
	}
}

logger.info(`ADB_SERVER_VERSION=${VERSION}`);

global.metainfo.version = VERSION;
// Read server bytes: prefer user-provided local file, otherwise use bundled BIN
// Replace the previous immediate-read (and process.exit) with a loader that polls every 30s
async function loadServerBinary() {
	const RETRY_MS = 30_000; // 30 seconds
	if (userProvidedLocalServerPath) {
		logger.info(`User provided DEFAULT_SERVER_PATH: ${LOCAL_SERVER_PATH_RESOLVED} — attempting to load`);
		while (true) {
			try {
				await fs.access(LOCAL_SERVER_PATH_RESOLVED);
				logger.info(`Using local DEFAULT_SERVER_PATH: ${LOCAL_SERVER_PATH_RESOLVED}`);
				return await fs.readFile(LOCAL_SERVER_PATH_RESOLVED);
			} catch (err) {
				logger.error(`DEFAULT_SERVER_PATH not found or not accessible: ${LOCAL_SERVER_PATH_RESOLVED}`);
				logger.info(`Retrying to load DEFAULT_SERVER_PATH in ${RETRY_MS/1000}s...`);
				// wait and retry
				await new Promise((r) => setTimeout(r, RETRY_MS));
			}
		}
	} else {
		// Use bundled BIN — if this fails, it's a fatal condition (unlikely), but we will also poll
		try {
			return await fs.readFile(BIN);
		} catch (err) {
			logger.error(`Failed to read embedded BIN for scrcpy server: ${String(err)}`);
			logger.info('Will retry reading embedded BIN every 30s');
			while (true) {
				try { return await fs.readFile(BIN); } catch (e) { logger.error('Retry read BIN failed:', e?.message || e); await new Promise((r) => setTimeout(r, 30_000)); }
			}
		}
	}
}

const server = await loadServerBinary();

// helper sleep
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// helper to add a timeout to any promise
function withTimeout(promise, ms = 10000, errMsg = null) {
	return new Promise((resolve, reject) => {
		let settled = false;
		const timer = setTimeout(() => {
			if (settled) return;
			settled = true;
			reject(new Error(errMsg || `Timed out after ${ms}ms`));
		}, ms);
		promise.then((v) => {
			if (settled) return;
			settled = true; clearTimeout(timer); resolve(v);
		}).catch((e) => {
			if (settled) return; settled = true; clearTimeout(timer); reject(e);
		});
	});
}

// Support multiple ADB servers via env var ADB_SERVER_LIST (comma-separated host:port entries).
// Fallback to localhost:5037 for backward compatibility.
function getAdbServerEntries() {
	const adbServerListStr = (process.env.ADB_SERVER_LIST || process.env.ADB_SERVERS || 'localhost:5037').toString();
	return adbServerListStr.split(',').map(s => s.trim()).filter(Boolean);
}

// Create pools function (attempts once)
async function createPoolsOnce(adbServerEntriesParam) {
	const adbServerEntries = Array.isArray(adbServerEntriesParam) && adbServerEntriesParam.length ? adbServerEntriesParam : (typeof adbServerEntriesParam === 'string' && adbServerEntriesParam.length ? adbServerEntriesParam.split(',').map(s => s.trim()).filter(Boolean) : getAdbServerEntries());
	const results = await Promise.all(adbServerEntries.map(async (entry) => {
		const parts = entry.split(':');
		const host = parts[0] || 'localhost';
		const port = parts[1] ? Number(parts[1]) : 5037;
		try {
			const connector = new AdbServerNodeTcpConnector({ host, port });
			const client = new AdbServerClient(connector);
			const key = `${host}:${port}`;
			// Verify connectivity by requesting server features with a timeout
			try {
				const features = await withTimeout(client.getServerFeatures(), 30000, `Timeout connecting to ADB server ${host}:${port}`);
				logger.info(`Configured ADB server pool ${key} (features:${Object.keys(features||{}).length})`);
				return { host, port, key, connector, client };
			} catch (e) {
				try { if (connector && typeof connector.close === 'function') connector.close(); } catch (closeErr) { logger.debug(`Failed to close connector for ${key}: ${closeErr?.message || closeErr}`); }
				logger.error(`Skipping ADB server ${key}: ${e?.message || e}`);
				return null;
			}
		} catch (e) {
			logger.error(`Failed to create ADB server connector for ${entry}: ${e?.message || e}`);
			return null;
		}
	}));
	return results.filter(Boolean);
}

// Ensure we have at least one pool — poll every 30s instead of exiting the process
let pools = [];
const POOL_RETRY_MS = 30_000; // 30s
while (true) {
	pools = await createPoolsOnce();
	if (pools && pools.length > 0) {
		logger.info(`Initialized ${pools.length} ADB server pool(s).`);
		break;
	}
	logger.error('No valid ADB server pools configured or reachable. Will retry in 30s.');
	await sleep(POOL_RETRY_MS);
}

// convenience: default pool (first entry) to preserve previous single-host behavior
let defaultPool = pools[0];

// --- Automatic pool health monitoring and auto-refresh on repeated failures ---
const POOL_FAILURE_THRESHOLD = 3; // number of consecutive failures to trigger refresh
const POOL_FAILURE_WINDOW_MS = 60_000; // time window to consider failures (1 minute)
const REFRESH_DEBOUNCE_MS = 30_000; // minimum time between auto-refresh attempts
let poolFailures = new Map(); // key -> { count, firstTs, lastTs }
let refreshInProgress = false;
let lastRefreshAttempt = 0;

function recordPoolFailure(key, err) {
	try {
		const now = Date.now();
		const s = poolFailures.get(key) || { count: 0, firstTs: now, lastTs: now };
		// if outside window, reset
		if (now - s.firstTs > POOL_FAILURE_WINDOW_MS) {
			s.count = 1; s.firstTs = now; s.lastTs = now;
		} else {
			s.count += 1; s.lastTs = now;
		}
		poolFailures.set(key, s);
		logger.debug(`Pool failure recorded for ${key}: count=${s.count} (err=${String(err)})`);
		// decide whether to schedule refresh
		if (s.count >= POOL_FAILURE_THRESHOLD) {
			const sinceLast = now - lastRefreshAttempt;
			if (!refreshInProgress && sinceLast > REFRESH_DEBOUNCE_MS) {
				logger.warn(`Pool ${key} failed ${s.count} times within ${POOL_FAILURE_WINDOW_MS}ms — scheduling refresh`);
				doAutoRefresh().catch((e) => logger.error('autoRefresh failed:', e?.message || e));
			} else {
				logger.debug(`Auto-refresh suppressed for ${key} (refreshInProgress=${refreshInProgress}, sinceLast=${sinceLast})`);
			}
		}
	} catch (e) { logger.debug('recordPoolFailure internal error', e?.message || e); }
}

async function doAutoRefresh() {
	if (refreshInProgress) return;
	refreshInProgress = true;
	lastRefreshAttempt = Date.now();
	logger.info('Auto-refreshing ADB server pools due to repeated failures');
	try {
		const newPools = await createPoolsOnce();
		if (!newPools || newPools.length === 0) {
			logger.warn('autoRefresh: no pools created; keeping existing pools');
			return { ok: false, message: 'no-pools-found' };
		}
		const newKeys = new Set(newPools.map(p => p.key));
		for (const old of pools) {
			if (!newKeys.has(old.key)) {
				try { if (old.connector && typeof old.connector.close === 'function') old.connector.close(); } catch (e) { logger.debug(`autoRefresh: failed to close old connector ${old.key}: ${e?.message || e}`); }
				try { if (old.client && typeof old.client.close === 'function') old.client.close(); } catch (e) { logger.debug(`autoRefresh: failed to close old client ${old.key}: ${e?.message || e}`); }
			}
		}
		pools = newPools;
		defaultPool = pools[0];
		// reset failure counters for refreshed pools
		for (const k of poolFailures.keys()) {
			if (newKeys.has(k)) poolFailures.delete(k);
		}
		logger.info(`autoRefresh: replaced pools, new count=${pools.length}`);
		return { ok: true, count: pools.length };
	} finally { refreshInProgress = false; }
}

// Periodic health probe to proactively detect failing pools
const POOL_HEALTH_PROBE_MS = 30_000; // probe every 30s
setInterval(async () => {
	for (const p of pools) {
		try {
			await withTimeout(p.client.getServerFeatures(), 10_000, `health probe timeout for ${p.key}`);
			// healthy => clear failure counter
			if (poolFailures.has(p.key)) poolFailures.delete(p.key);
		} catch (e) {
			logger.debug(`Health probe failed for pool=${p.key}: ${e?.message || e}`);
			recordPoolFailure(p.key, e);
		}
	}
}, POOL_HEALTH_PROBE_MS);

// helper to find the pool by key
function findPoolByKey(key) {
	return pools.find(p => p.key === key) || null;
}

// helper to find the pool that hosts a given serial (searches metainfo cache first, then probes all servers)
async function findPoolForSerial(serial) {
	logger.info(`findPoolForSerial: resolving pool for identifier=${serial}`);
	if (!serial) return null;
	// if already server-prefixed like host:port/serial or key/serial
	if (serial.includes('/')) {
		const [maybeKey, maybeSerial] = serial.split('/', 2);
		const pool = findPoolByKey(maybeKey);
		if (pool) return { pool, serial: maybeSerial };
	}

	// First, if the identifier exactly matches a unique device name (host:port/serial), resolve quickly
	if (global && global.metainfo && Array.isArray(global.metainfo.devices)) {
		const byName = global.metainfo.devices.find(d => d && d.name === serial);
		if (byName) {
			const pool = findPoolByKey(byName._serverKey || `${byName._serverHost}:${byName._serverPort}`) || defaultPool;
			logger.info(`findPoolForSerial: resolved by unique name '${serial}' -> pool=${pool.key}`);
			return { pool, serial: byName.serial };
		}

		// Next, find devices by bare serial
		const matches = global.metainfo.devices.filter(d => d && d.serial === serial);
		if (matches.length === 1) {
			const d = matches[0];
			const pool = findPoolByKey(d._serverKey || `${d._serverHost}:${d._serverPort}`) || defaultPool;
			logger.info(`findPoolForSerial: single metainfo match for serial='${serial}' -> pool=${pool.key}`);
			return { pool, serial: d.serial };
		} else if (matches.length > 1) {
			// Ambiguous: multiple devices with same serial across different pools
			logger.warn(`findPoolForSerial: ambiguous serial '${serial}' matched ${matches.length} devices across pools. Prefer explicit unique name 'host:port/serial'. Attempting deterministic fallback.`);
			// Prefer device that belongs to defaultPool if present
			let chosen = matches.find(m => m._serverKey === defaultPool.key) || matches[0];
			const pool = findPoolByKey(chosen._serverKey || `${chosen._serverHost}:${chosen._serverPort}`) || defaultPool;
			logger.info(`findPoolForSerial: ambiguity resolved to pool=${pool.key} (device name=${chosen.name})`);
			return { pool, serial: chosen.serial };
		}
	}

	// Fallback: probe each server for device serial (network probe)
	for (const p of pools) {
		try {
			const ds = await p.client.getDevices();
			if (Array.isArray(ds) && ds.find(d => d && d.serial === serial)) return { pool: p, serial };
		} catch (e) {
			logger.error(`findPoolForSerial probe failed for pool=${p.key}: ${e?.message || e}`);
			recordPoolFailure(p.key, e);
		}
	}

	logger.debug(`findPoolForSerial: could not resolve pool for identifier=${serial}`);
	return null;
}

// convenience wrapper to use defaultPool.client in places that previously used serverClient
if (defaultPool.hasOwnProperty('client')){
	logger.info(`Setting up default serverClient for pool ${defaultPool.key}`);
	// defaultPool.client is available for legacy code paths when needed
} else {
	throw new Error('defaultPool does not have a client property');
}

// Track pushes per serial and in-flight promises
const pushedBySerial = new Set();
const pushInFlightBySerial = new Map();
const pushInFlightByInstance = new WeakMap();

function resolveSerialFromAdbInstance(adbInstance) {
	try {
		if (global && global.metainfo && Array.isArray(global.metainfo.devices)) {
			for (const d of global.metainfo.devices) {
				if (d && d.adb === adbInstance && d.serial) return d.serial;
			}
		}
		if (adbInstance) {
			if (adbInstance.transport && adbInstance.transport.serial) return adbInstance.transport.serial;
			if (adbInstance._transport && adbInstance._transport.serial) return adbInstance._transport.serial;
			if (typeof adbInstance.serial === 'string') return adbInstance.serial;
		}
	} catch (e) {
		logger.debug(`resolveSerialFromAdbInstance error:', ${e?.message || e}`);
	}
	return null;
}

const pushServer = async (adbInstance, force = false) => {
	logger.info(`Pushing scrcpy server to device if not already present with Instance: ${JSON.stringify(adbInstance)}`);
	const serial = resolveSerialFromAdbInstance(adbInstance);
	logger.info(`Got serial from adb instance: ${serial}`);
	// probe remote server file via ls -l to verify existence/size
	const probeRemoteServer = async (adbInst) => {
		try {
			if (!adbInst || !adbInst.subprocess || typeof adbInst.subprocess.exec !== 'function') return { exists: null, size: null };
			const out = await adbInst.subprocess.exec(["ls", "-l", DEVICE_SERVER_PATH]);
			const outStr = Array.isArray(out) ? out.join('\n') : String(out);
			if (/No such file|No such file or directory/i.test(outStr)) return { exists: false, size: null };
			const lines = outStr.split(/\r?\n/).filter(Boolean);
			if (!lines.length) return { exists: null, size: null };
			const first = lines[0];
			const m = first.match(/^\S+\s+\d+\s+\S+\s+\S+\s+(\d+)/);
			if (m && m[1]) return { exists: true, size: Number(m[1]) };
			// fallback - find numeric token
			const tokens = first.split(/\s+/);
			for (let t of tokens) if (/^\d+$/.test(t)) return { exists: true, size: Number(t) };
			logger.info("probeRemoteServer: could not parse ls -l output for size");
			return { exists: true, size: null };
		} catch (e) {
			logger.error(`probeRemoteServer failed: ${e?.message || e}`);
		}
	};

	if (serial) {
		logger.info(`pushServer: resolved serial=${serial}`);
		if (pushedBySerial.has(serial) && !force) {
			try {
				const remote = await probeRemoteServer(adbInstance);
				const localSize = server ? (server.byteLength || server.length || null) : null;
				if (remote.exists === true && remote.size != null && localSize != null && remote.size === localSize) {
					logger.debug(`pushServer: server already pushed to device serial=${serial}, remote file exists and size matches, skipping`);
					return;
				}
				logger.warn(`pushServer: server previously marked pushed for serial=${serial} but remote file missing or size mismatch (remote=${remote.size} local=${localSize}); re-pushing`);
				pushedBySerial.delete(serial);
			} catch (e) {
				logger.debug('pushServer: verification probe failed, proceeding to re-push', e?.message || e);
				pushedBySerial.delete(serial);
			}
		}

		if (pushInFlightBySerial.has(serial)) {
			if (!force) {
				logger.debug(`pushServer: push already in-flight for serial=${serial}, awaiting existing promise`);
				return await pushInFlightBySerial.get(serial);
			} else {
				logger.debug(`pushServer: forced push requested for serial=${serial}, waiting for any in-flight push to finish before forcing`);
				try { await pushInFlightBySerial.get(serial); } catch (e) { /* ignore */ }
				pushedBySerial.delete(serial);
			}
		}

		const promise = (async () => {
			logger.debug(`Pushing scrcpy server to device serial=${serial} path: ${DEVICE_SERVER_PATH} (force=${!!force})`);
			try {
				await AdbScrcpyClient.pushServer(
					adbInstance,
					new ReadableStream({
						start(controller) { controller.enqueue(new Consumable(server)); controller.close(); },
					})
						.pipeThrough(new DistributionStream(ADB_SYNC_MAX_PACKET_SIZE))
						.pipeThrough(new ProgressStream((progress) => logger.debug(`scrcpy server upload progress: ${progress}`))),
					DEVICE_SERVER_PATH,
				);
				pushedBySerial.add(serial);
				logger.debug(`pushServer: marked serial=${serial} as pushed`);
				logger.debug('scrcpy server pushed successfully');
			} catch (err) {
				logger.error('Error while pushing scrcpy server to device:', err);
				throw err;
			} finally { pushInFlightBySerial.delete(serial); }
		})();
		pushInFlightBySerial.set(serial, promise);
		return await promise;
	} else {
		if (pushInFlightByInstance.has(adbInstance)) {
			logger.debug('pushServer: push in-flight for this adb instance, awaiting it');
			return await pushInFlightByInstance.get(adbInstance);
		}
		const promise = (async () => {
			logger.debug(`Pushing scrcpy server (fallback by instance) to device path: ${DEVICE_SERVER_PATH} (force=${!!force})`);
			try {
				await AdbScrcpyClient.pushServer(
					adbInstance,
					new ReadableStream({ start(controller) { controller.enqueue(new Consumable(server)); controller.close(); } })
						.pipeThrough(new DistributionStream(ADB_SYNC_MAX_PACKET_SIZE))
						.pipeThrough(new ProgressStream((progress) => logger.debug(`scrcpy server upload progress: ${progress}`))),
					DEVICE_SERVER_PATH,
				);
				logger.debug('scrcpy server pushed successfully (fallback)');
			} catch (err) { logger.error('Error while pushing scrcpy server to device (fallback):', err); throw err; }
			finally { try { pushInFlightByInstance.delete(adbInstance); } catch (e) {} }
		})();
		try { pushInFlightByInstance.set(adbInstance, promise); } catch (e) { logger.debug('pushServer: could not set pushInFlightByInstance', e?.message || e); }
		return await promise;
	}
};

class AdbTcpService {
	numOfTrials = 10;
	async getFeatures() {
		// Aggregate server features from all pools (use first non-error result)
		const results = [];
		for (const p of pools) {
			try {
				const f = await p.client.getServerFeatures();
				results.push({ pool: p.key, features: f });
			} catch (e) {
				logger.debug(`getFeatures: pool ${p.key} failed: ${e?.message || e}`);
				recordPoolFailure(p.key, e);
			}
		}
		// return aggregated: prefer defaultPool.features if available else first
		if (results.length) return results[0].features;
		throw new Error('Could not retrieve server features from any configured ADB server');
	}
	async getDevices() {
        // Query each pool for devices and tag them with pool metadata
        const all = [];
        for (const p of pools) {
            try {
                const ds = await p.client.getDevices();
                if (Array.isArray(ds)) {
                    for (const d of ds) {
                        if (!d) continue;
                        d._serverKey = p.key;
                        d._serverHost = p.host;
                        d._serverPort = p.port;
                        all.push(d);
                    }
                }
            } catch (e) {
                logger.debug(`getDevices: pool ${p.key} failed: ${e?.message || e}`);
                recordPoolFailure(p.key, e);
            }
        }
        // Deduplicate by poolKey+serial (keep first seen for that pool+serial)
        const seen = new Set();
        const unique = [];
        for (const d of all) {
            if (!d || !d.serial) continue;
            const uniqueKey = `${d._serverKey}/${d.serial}`;
            if (seen.has(uniqueKey)) continue;
            seen.add(uniqueKey);
            unique.push(d);
        }
        return unique;
    }

	async connectToDevice(serial) {
		// Determine which pool hosts this serial
		logger.info(`connectToDevice: resolving pool for serial=${serial}`);
		let poolInfo = null;
		if (typeof serial === 'string' && serial.includes('/')) {
			// allow explicit pool key prefix like host:port/serial
			const [maybeKey, maybeSerial] = serial.split('/', 2);
			const pool = findPoolByKey(maybeKey);
			if (pool) poolInfo = { pool, serial: maybeSerial };
		}else {
			logger.info(`connectToDevice: serial=${serial} has no explicit pool key prefix, searching metainfo cache`);
		}
		if (!poolInfo) {
			poolInfo = await findPoolForSerial(serial);
		}
		if (!poolInfo) {
			// last resort: use defaultPool and attempt to create transport
			logger.info(`connectToDevice: could not find pool for serial ${serial}, using default pool ${defaultPool.key}`);
			poolInfo = { pool: defaultPool, serial };
		}
		const { pool } = poolInfo;
		logger.info(`Connecting to device serial=${poolInfo.serial} via ADB server pool=${pool.key} with poolInfo=${JSON.stringify(poolInfo)}`);
		// create transport on the selected pool's client
		const transport = await pool.client.createTransport({ serial: poolInfo.serial });
		const adb = new Adb(transport);
		// Create a stable unique name and a human-friendly displayName for the device
		const uniqueName = `${pool.host}:${pool.port}/${poolInfo.serial}`;
		const displayName = `${pool.host}`;
		const deviceModel = { serial: poolInfo.serial, name: uniqueName, displayName, transport, adb, displays: [], encoders: [], _serverKey: pool.key, _serverHost: pool.host, _serverPort: pool.port };
		try { await pushServer(adb); } catch (err) {
			logger.error(`Initial pushServer failed in connectToDevice: ${err}`);
			throw err;
		}
		return deviceModel
	}

	async getDeviceDisplays(deviceAdb) {
		let result = [];
		let trial = 0;
		// push server once before trials
		logger.info(`Getting device displays for deficeAdb: ${JSON.stringify(deviceAdb)}`)
		try { await pushServer(deviceAdb); } catch (err) { logger.error('Initial pushServer failed in getDeviceDisplays:', err); }
		// optional: try to verify file exists on device (best-effort)
		logger.info('Verifying scrcpy server file existence on device before getDeviceDisplays');
		try { if (deviceAdb.subprocess && typeof deviceAdb.subprocess.exec === 'function') { const check = await deviceAdb.subprocess.exec(["ls", "-l", DEVICE_SERVER_PATH]); logger.debug(`device ls output: ${JSON.stringify(check)}`); } } catch (e) { logger.debug('Device file existence check failed or not supported:', e?.message || e); }
		while (!result?.length && trial < this.numOfTrials) {
			try {
				logger.debug(`Attempt ${trial + 1} to get displays`);
				const minimalInitForList = { cleanup: true, tunnelForward: true };
				const displays = await AdbScrcpyClient.getDisplays(deviceAdb, DEVICE_SERVER_PATH, new AdbScrcpyOptionsLatest(minimalInitForList, { version: VERSION }));
				logger.debug(`getDisplays returned: ${JSON.stringify(displays)}`);
				result = displays || [];
				if (!result.length) {
					logger.warn(`getDisplays returned empty on attempt ${trial + 1}; attempting to re-push scrcpy server and retry.`);
					try { await pushServer(deviceAdb, true); await new Promise((r) => setTimeout(r, 300)); } catch (pushErr) { logger.error('Re-push scrcpy server after empty displays failed:', pushErr); }
				}
			} catch (err) {
				logger.error('Error while getting displays:', err);
				try { logger.debug('Retrying pushServer after getDisplays error'); await pushServer(deviceAdb); } catch (pushErr) { logger.error('Retry pushServer failed:', pushErr); }
			}
			trial++;
			if (!result?.length) await new Promise((r) => setTimeout(r, 250));
		}
		logger.info(`getDeviceDisplays in trial=${trial} resultCount=${result?.length || 0}`);
		return result;
	}

	async getDeviceEncoders(deviceAdb) {
		let result = [];
		let trial = 0;
		try { await pushServer(deviceAdb); } catch (err) { logger.error('Initial pushServer failed in getDeviceEncoders:', err); }
		try { if (deviceAdb.subprocess && typeof deviceAdb.subprocess.exec === 'function') { const check = await deviceAdb.subprocess.exec(["ls", "-l", DEVICE_SERVER_PATH]); logger.debug(`device ls output: ${JSON.stringify(check)}`); } } catch (e) { logger.debug('Device file existence check failed or not supported:', e?.message || e); }
		while (!result?.length && trial < this.numOfTrials) {
			try {
				logger.debug(`Attempt ${trial + 1} to get encoders`);
				const minimalInitForList = { cleanup: true, tunnelForward: true };
				const encoders = await AdbScrcpyClient.getEncoders(deviceAdb, DEVICE_SERVER_PATH, new AdbScrcpyOptionsLatest(minimalInitForList, { version: VERSION }));
				logger.debug(`getEncoders returned: ${JSON.stringify(encoders)}`);
				result = encoders || [];
				if (!result.length) {
					logger.warn(`getEncoders returned empty on attempt ${trial + 1}; attempting to re-push scrcpy server and retry.`);
					try { await pushServer(deviceAdb, true); await new Promise((r) => setTimeout(r, 300)); } catch (pushErr) { logger.error('Re-push scrcpy server after empty encoders failed:', pushErr); }
				}
			} catch (err) {
				logger.error('Error while getting encoders:', err);
				try { logger.debug('Retrying pushServer after getEncoders error'); await pushServer(deviceAdb); } catch (pushErr) { logger.error('Retry pushServer failed:', pushErr); }
			}
			trial++;
			if (!result?.length) await new Promise((r) => setTimeout(r, 250));
		}
		logger.info(`getDeviceEncoders in trial=${trial} resultCount=${result?.length || 0}`);
		return result;
	}

	async start(deviceAdb, user, _attempt = 0) {
		const {
			audio,
			audioCodec,
			audioEncoder,
			video,
			videoCodec,
			videoEncoder,
			videoBitRate,
			displayId,
			maxSize,
			maxFps,
		} = user.ws;
		// Build sanitized init
		const init = {};
		if (typeof audio !== 'undefined') init.audio = !!audio;
		if (audioCodec) init.audioCodec = audioCodec;
		// Only set audioEncoder when audioCodec is not 'raw' and audioEncoder is not 'raw'
		if (audioEncoder && String(audioEncoder).toLowerCase() !== 'raw' && String(init.audioCodec || '').toLowerCase() !== 'raw') {
			init.audioEncoder = audioEncoder;
		}
		if (typeof video !== 'undefined') init.video = !!video;
		if (videoCodec) init.videoCodec = videoCodec;
		if (videoEncoder) init.videoEncoder = videoEncoder;
		if (typeof videoBitRate !== 'undefined' && videoBitRate !== null) init.videoBitRate = Number(videoBitRate);
		if (typeof displayId !== 'undefined' && displayId !== null) init.displayId = Number(displayId);
		if (typeof maxSize !== 'undefined' && maxSize !== null) init.maxSize = Number(maxSize);
		if (typeof maxFps !== 'undefined' && maxFps !== null) init.maxFps = Number(maxFps);
		if (ScrcpyInstanceId && ScrcpyInstanceId.random) init.scid = ScrcpyInstanceId.random();
		init.cleanup = true;
		init.tunnelForward = true;

		// ensure server binary is present on the device
		await pushServer(deviceAdb);

		// Try to query device encoders to select a compatible encoder (prefer device-supported names)
		let deviceEncoders = [];
		try {
			deviceEncoders = await this.getDeviceEncoders(deviceAdb) || [];
			// normalize to only video encoders
			deviceEncoders = deviceEncoders.filter(e => e && e.type === 'video');
		} catch (e) {
			logger.debug('Could not obtain device encoders before scrcpy.start:', e?.message || e);
		}
		const availableEncoderNames = deviceEncoders.map(e => e && e.name).filter(Boolean);

		// Helper to pick encoder for the given codec. Prefer c2.* if present, else any matching codec.
		const pickEncoderForCodec = (codec) => {
			if (!deviceEncoders || !deviceEncoders.length) return null;
			let candidates = deviceEncoders.filter(e => String(e.codec || '').toLowerCase() === String(codec || '').toLowerCase());
			if (!candidates.length) return null;
			// prefer c2.* names (common on modern Android)
			const preferred = candidates.find(c => /(^|\.|\-)c2\.|c2\./i.test(c.name)) || candidates[0];
			return preferred.name;
		};

		// If user provided a videoEncoder with an '@' prefix (like TinyH264@c2.android.avc.encoder), prefer RHS if available
		if (videoEncoder) {
			const vEncStr = String(videoEncoder);
			if (vEncStr.includes('@')) {
				const rhs = vEncStr.split('@').pop();
				if (availableEncoderNames.includes(rhs)) {
					init.videoEncoder = rhs;
					logger.info(`Mapped requested videoEncoder '${videoEncoder}' -> using device encoder '${rhs}'`);
				} else if (availableEncoderNames.includes(vEncStr)) {
					init.videoEncoder = vEncStr;
					logger.info(`Using requested videoEncoder as-is: '${vEncStr}'`);
				} else {
					// fallback: try to pick a device encoder for the given codec
					const picked = pickEncoderForCodec(videoCodec || 'h264');
					if (picked) {
						init.videoEncoder = picked;
						logger.warn(`Requested videoEncoder '${videoEncoder}' not available; falling back to device encoder '${picked}'`);
					} else {
						logger.warn(`Requested videoEncoder '${videoEncoder}' not available and no matching fallback found`);
					}
				}
			} else {
				// videoEncoder provided without '@', check availability
				if (availableEncoderNames.includes(vEncStr)) {
					init.videoEncoder = vEncStr;
					logger.info(`Using requested videoEncoder '${vEncStr}'`);
				} else {
					const picked = pickEncoderForCodec(videoCodec || 'h264');
					if (picked) { init.videoEncoder = picked; logger.warn(`Requested videoEncoder '${vEncStr}' not available; falling back to '${picked}'`); }
				}
			}
		} else {
			// No explicit videoEncoder requested - pick sensible default for codec
			const auto = pickEncoderForCodec(videoCodec || 'h264');
			if (auto) { init.videoEncoder = auto; logger.info(`No videoEncoder requested; auto-selected '${auto}' for codec='${videoCodec || 'h264'}'`); }
		}

		// audioEncoder: if provided and contains '@', use RHS if device supports it, else try to pick by codec
		if (audioEncoder) {
			const aEncStr = String(audioEncoder);
			try {
				// query audio encoders list
				let audioEncList = [];
				try { audioEncList = (await this.getDeviceEncoders(deviceAdb)) || []; } catch (e) { /* ignore */ }
				audioEncList = audioEncList.filter(e => e && e.type === 'audio');
				const audioNames = audioEncList.map(e => e.name).filter(Boolean);
				if (aEncStr.includes('@')) {
					const rhs = aEncStr.split('@').pop();
					if (audioNames.includes(rhs)) { init.audioEncoder = rhs; logger.info(`Mapped requested audioEncoder '${audioEncoder}' -> '${rhs}'`); }
					else if (audioNames.includes(aEncStr)) { init.audioEncoder = aEncStr; }
					else { logger.debug(`Requested audioEncoder '${audioEncoder}' not available on device`); }
				} else {
					if (audioNames.includes(aEncStr)) init.audioEncoder = aEncStr; else logger.debug(`Requested audioEncoder '${audioEncoder}' not available on device`);
				}
			} catch (e) { logger.debug('audioEncoder selection failed:', e?.message || e); }
		}

		// finalize videoEncoder selection and normalize forms like 'TinyH264@c2.android.avc.encoder'
		let chosenVideoEncoder = null;
		if (init.videoEncoder) {
			const v = String(init.videoEncoder);
			if (v.includes('@')) {
				const rhs = v.split('@').pop();
				if (availableEncoderNames.includes(rhs)) chosenVideoEncoder = rhs;
				else if (availableEncoderNames.includes(v)) chosenVideoEncoder = v;
				else chosenVideoEncoder = null;
			} else if (availableEncoderNames.includes(v)) {
				chosenVideoEncoder = v;
			} else {
				chosenVideoEncoder = null;
			}
		} else {
			chosenVideoEncoder = pickEncoderForCodec(videoCodec || 'h264');
		}
		if (chosenVideoEncoder) {
			init.videoEncoder = chosenVideoEncoder;
			logger.info(`Final selected videoEncoder='${chosenVideoEncoder}'`);
		} else {
			// ensure we don't pass unsupported compound name
			if (init.videoEncoder && String(init.videoEncoder).includes('@')) delete init.videoEncoder;
			logger.info('No compatible videoEncoder found; letting scrcpy choose default');
		}

		// finalize audio encoder similarly
		let chosenAudioEncoder = null;
		if (init.audioEncoder) {
			const a = String(init.audioEncoder);
			if (a.includes('@')) {
				const rhs = a.split('@').pop();
				// reuse audio list fetched earlier
				// audioEncList may not exist here, fetch quickly
				try {
					let audioEncList = (await this.getDeviceEncoders(deviceAdb)) || [];
					audioEncList = audioEncList.filter(e => e && e.type === 'audio');
					const audioNames = audioEncList.map(e => e.name).filter(Boolean);
					if (audioNames.includes(rhs)) chosenAudioEncoder = rhs;
					else if (audioNames.includes(a)) chosenAudioEncoder = a;
				} catch (e) { logger.debug('audio encoder finalization failed:', e?.message || e); }
			} else {
				chosenAudioEncoder = init.audioEncoder;
			}
		}
		if (chosenAudioEncoder) {
			init.audioEncoder = chosenAudioEncoder;
			logger.info(`Final selected audioEncoder='${chosenAudioEncoder}'`);
		} else if (init.audioEncoder && String(init.audioEncoder).includes('@')) {
			delete init.audioEncoder;
			logger.info('No compatible audioEncoder found; letting scrcpy choose default');
		}

		// Create options after we possibly adjusted encoders
		const options = new AdbScrcpyOptions2_1(init, { version: VERSION });

		logger.debug(`Using encoders: video='${init.videoEncoder || ''}' audio='${init.audioEncoder || ''}' availableVideoEncoders=${JSON.stringify(availableEncoderNames)}`);

		// ensure server binary is present on the device
		await pushServer(deviceAdb);
		try {
			logger.info(`Starting scrcpy client with options: ${JSON.stringify(options)} \n and deviceAdb: ${JSON.stringify(deviceAdb)} \n and server path: ${DEVICE_SERVER_PATH}`);
			const client = await AdbScrcpyClient.start(deviceAdb, DEVICE_SERVER_PATH, options);
			// read some initial output lines from client.output to help debugging
			let initialOutputLines = [];
			try {
				if (client && client.output && typeof client.output.getReader === 'function') {
					const reader = client.output.getReader();
					// read up to 40 lines, with a short timeout for each
					for (let i = 0; i < 40; i++) {
						const p = reader.read();
						const r = await Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error('output-read-timeout')), 300))]);
						if (r && r.done) { break; }
						if (r && r.value) initialOutputLines.push(String(r.value));
					}
					try { reader.releaseLock(); } catch (e) {}
				}
			} catch (e) {
				logger.debug(`Reading initial client.output failed: ${e?.message || e}`);
			}

			// richer logging / validation: class instances often stringify to {} — inspect prototype/methods and hidden properties
			import('node:util').then((util) => {
				try {
					const proto = client && Object.getPrototypeOf(client) ? Object.getOwnPropertyNames(Object.getPrototypeOf(client)) : [];
					logger.info(`Got scrcpy client: type=${typeof client} protoKeys=${JSON.stringify(proto.slice(0,50))}`);
					logger.debug(`Inspect client (showHidden, depth=2): ${util.inspect(client, { showHidden: true, depth: 2 })}`);
					if (initialOutputLines.length) logger.debug(`Initial client.output lines:\n${initialOutputLines.join('\n')}`);
				} catch (e) { logger.debug('Error inspecting scrcpy client', e?.message || e); }
			});

			// Validate client has at least one expected API (best-effort check)
			const protoNames = client && Object.getPrototypeOf(client) ? Object.getOwnPropertyNames(Object.getPrototypeOf(client)) : [];
			const hasClose = client && typeof client.close === 'function';
			const hasOutput = client && client.output;
			const controlRequested = options && options.value && options.value.control === true;
			const hasController = !!(client && client.controller);
			logger.debug(`scrcpy client validation: hasClose=${hasClose} hasOutput=${!!hasOutput} controlRequested=${controlRequested} hasController=${hasController}`);

			// If control was requested, ensure controller exists
			if (!client || !hasClose || !hasOutput || (controlRequested && !hasController)) {
				// collect diagnostics and throw detailed error to be visible to caller
				const details = { clientTruthy: !!client, protoKeys: protoNames.slice(0,50), hasClose, hasOutput: !!hasOutput, controlRequested, hasController, server_length: server ? (server.byteLength || server.length || null) : null, initialOutputLines };
				try {
					if (deviceAdb && deviceAdb.subprocess && typeof deviceAdb.subprocess.exec === 'function') {
						details.logcat = await deviceAdb.subprocess.exec(["logcat", "-d", "-t", "200"]);
						details.device_ls = await deviceAdb.subprocess.exec(["ls", "-l", DEVICE_SERVER_PATH]);
					}
				} catch (e) {
					logger.debug('Diagnostics collection failed', e?.message || e);
				}
				const err = new Error('AdbScrcpyClient.start returned unexpected/insufficient client object');
				err.details = details;
				throw err;
			}

			// Now attempt to ensure video/audio streams are available (if requested). Use short timeouts to fail fast.
			try {
				if (options && options.value) {
					const checks = [];
					if (options.value.video === true && client.videoStream) {
						checks.push((async () => {
							try {
								// await videoStream promise with timeout
								await withTimeout(client.videoStream, 5000, 'video-stream-creation-timeout');
							} catch (e) { throw new Error('videoStream failed: ' + (e?.message || String(e))); }
						})());
					}
					if (options.value.audio === true && client.audioStream) {
						checks.push((async () => {
							try {
								await withTimeout(client.audioStream, 5000, 'audio-stream-creation-timeout');
							} catch (e) { throw new Error('audioStream failed: ' + (e?.message || String(e))); }
						})());
					}
					if (checks.length) await Promise.all(checks);
				}
			} catch (e) {
				// collect diagnostics and close client
				let diag = { message: e?.message || String(e), initialOutputLines };
				try {
					if (deviceAdb && deviceAdb.subprocess && typeof deviceAdb.subprocess.exec === 'function') {
						diag.logcat = await deviceAdb.subprocess.exec(["logcat", "-d", "-t", "200"]);
						diag.device_ls = await deviceAdb.subprocess.exec(["ls", "-l", DEVICE_SERVER_PATH]);
					}
				} catch (ee) { logger.debug('diag collection after stream failure failed', ee?.message || ee); }
				try { if (client && typeof client.close === 'function') await client.close(); } catch (ee) { logger.debug('client.close failed', ee?.message || ee); }
				const err = new Error('scrcpy stream creation failed: ' + diag.message); err.details = diag; throw err;
			}

			return { client, options };
		} catch (err) {
			// If this was the first attempt and audio was requested, retry without audio (some devices/servers fail on audio)
			if (_attempt === 0 && init.audio === true) {
				logger.warn('scrcpy start failed on initial attempt with audio enabled; retrying once with audio disabled');
				try {
					const initNoAudio = { ...init, audio: false };
					// Remove audioEncoder if present
					delete initNoAudio.audioEncoder;
					const optionsNoAudio = new AdbScrcpyOptions2_1(initNoAudio, { version: VERSION });
					logger.info('Retrying scrcpy start with options (audio disabled)');
					const client2 = await AdbScrcpyClient.start(deviceAdb, DEVICE_SERVER_PATH, optionsNoAudio);
					// perform the same validation checks as above (inspect, read output, stream checks)
					let initialOutputLines2 = [];
					try {
						if (client2 && client2.output && typeof client2.output.getReader === 'function') {
							const reader2 = client2.output.getReader();
							for (let i = 0; i < 40; i++) {
								const p2 = reader2.read();
								const r2 = await Promise.race([p2, new Promise((_, rej) => setTimeout(() => rej(new Error('output-read-timeout')), 300))]);
								if (r2 && r2.done) break;
								if (r2 && r2.value) initialOutputLines2.push(String(r2.value));
							}
							try { reader2.releaseLock(); } catch (e) {}
						}
					} catch (e2) { logger.debug('Reading initial client2.output failed:', e2?.message || e2); }

					// basic validation
					const hasClose2 = client2 && typeof client2.close === 'function';
					const hasOutput2 = client2 && client2.output;
					if (!client2 || !hasClose2 || !hasOutput2) {
						logger.error('Retry without audio still returned insufficient client');
						try { if (client2 && typeof client2.close === 'function') await client2.close(); } catch (e3) { logger.debug('client2.close failed', e3?.message || e3); }
						// fall through to original error handling below
					} else {
						// attempt stream checks for video only
						try {
							if (optionsNoAudio && optionsNoAudio.value && optionsNoAudio.value.video === true && client2.videoStream) {
								await withTimeout(client2.videoStream, 5000, 'video-stream-creation-timeout-retry');
							}
							logger.info('Retry without audio succeeded');
							return { client: client2, options: optionsNoAudio };
						} catch (e4) {
							logger.error('Retry without audio failed during stream checks', e4?.message || e4);
							try { if (client2 && typeof client2.close === 'function') await client2.close(); } catch (e5) { logger.debug('client2.close failed', e5?.message || e5); }
						}
					}
				} catch (retryErr) {
					logger.error('scrcpy retry (no-audio) failed:', retryErr?.message || retryErr);
				}
			}

			// original error handling: collect diagnostics and rethrow a detailed error
			const details = { message: err && err.message ? err.message : String(err), name: err && err.name ? err.name : 'Error', stack: err && err.stack ? err.stack : null, server_length: server ? (server.byteLength || server.length || null) : null, logcat: null, device_ls: null };
			try {
				if (deviceAdb && deviceAdb.subprocess && typeof deviceAdb.subprocess.exec === 'function') {
					details.logcat = await deviceAdb.subprocess.exec(["logcat", "-d", "-t", "200"]);
				}
			} catch (e) { logger.debug('Failed to collect logcat during scrcpy start error:', e?.message || e); }
			// Try to inspect server file on device
			try {
				if (deviceAdb && deviceAdb.subprocess && typeof deviceAdb.subprocess.exec === 'function') {
					details.device_ls = await deviceAdb.subprocess.exec(["ls", "-l", DEVICE_SERVER_PATH]);
				}
			} catch (e) { logger.debug('Failed to run ls on device for server file:', e?.message || e); }
			const e2 = new Error(`scrcpy start failed: ${details.message}`); e2.details = details; throw e2;
		}
	}

	async getDeviceAdb(deviceParam) {
		logger.info(`getDeviceAdb called with: ${JSON.stringify(deviceParam)}`);
		// deviceParam can be either a string (serial) or an object { host, port, serial, key, uniqueName }
		let serial = null;
		let uniqueName = null;
		let key = null;
		if (deviceParam && typeof deviceParam === 'object') {
			serial = deviceParam.serial || deviceParam.original || null;
			uniqueName = deviceParam.uniqueName || null;
			key = deviceParam.key || (deviceParam.host && deviceParam.port ? `${deviceParam.host}:${deviceParam.port}` : null);
		} else {
			serial = deviceParam;
		}

		// Helper to lookup in metainfo devices safely
		const lookupInMeta = () => {
			if (!global || !global.metainfo || !Array.isArray(global.metainfo.devices)) return null;
			// prefer exact uniqueName match
			if (uniqueName) {
				const byUnique = global.metainfo.devices.find((d) => d && (d.name === uniqueName || `${d._serverKey}/${d.serial}` === uniqueName));
				if (byUnique) return byUnique;
			}
			// try by serial or name
			if (serial) {
				const bySerial = global.metainfo.devices.find((d) => d && (d.serial === serial || d.name === serial || `${d._serverKey}/${d.serial}` === serial));
				if (bySerial) return bySerial;
			}
			return null;
		};

		// Try to find existing device model
		let deviceModel = lookupInMeta();
		if (!deviceModel) {
			// Refresh metainfo once and retry
			try {
				logger.debug('getDeviceAdb: refreshing metainfo for lookup');
				await this.metainfo();
				deviceModel = lookupInMeta();
			} catch (e) {
				logger.debug('getDeviceAdb: metainfo refresh failed', e?.message || e);
			}
		}

		// If we still don't have a deviceModel but key+serial were provided, attempt explicit connect to that pool
		if (!deviceModel && key && serial) {
			try {
				logger.info(`getDeviceAdb: attempting explicit connectToDevice for ${key}/${serial}`);
				const dm = await this.connectToDevice(`${key}/${serial}`);
				if (dm && dm.adb) return dm.adb;
				deviceModel = dm;
			} catch (e) {
				logger.debug(`getDeviceAdb explicit connectToDevice failed: ${e?.message || e}`);
			}
		}

		// If still not found, attempt generic connectToDevice(serial)
		if (!deviceModel && serial) {
			try {
				logger.info(`getDeviceAdb: attempting fallback connectToDevice for serial=${serial}`);
				const dm = await this.connectToDevice(serial);
				if (dm && dm.adb) return dm.adb;
				deviceModel = dm;
			} catch (e) {
				logger.debug(`getDeviceAdb fallback connectToDevice failed: ${e?.message || e}`);
			}
		}

		if (!deviceModel) {
			const id = serial || uniqueName || (deviceParam && deviceParam.original) || '<unknown>';
			throw new Error(`Device with serial = '${id}' is not connected.`);
		}

		logger.info(`getDeviceAdb: resolved device model: name=${deviceModel.name || '<noname>'} serial=${deviceModel.serial}`);
		return deviceModel.adb;
	}

	async setDeviceVolume(deviceAdb, volumeOrPayload, muted = false) {
		// Deprecated: this service no longer performs volume changes via adb subprocess.exec.
		// Volume control should be performed by injecting key codes via the scrcpy controller
		// using the `injectKeyCode` control message (AndroidKeyCode.VolumeUp=24, VolumeDown=25, MUTE=164).
		try {
			logger.debug('setDeviceVolume called but is deprecated. deviceSerial=', resolveSerialFromAdbInstance(deviceAdb), 'payload=', volumeOrPayload, 'muted=', muted);
		} catch (e) {
			logger.debug('setDeviceVolume called (could not resolve serial)', e?.message || e);
		}
		return { ok: false, error: 'deprecated-use-controller-injectKeyCode', detail: 'Use controller.injectKeyCode to simulate VolumeUp/VolumeDown/MUTE' };
	}

	async metainfo() {
		const [features, devices] = await Promise.all([ this.getFeatures(), this.getDevices() ]);
		// Connect using serverKey/serial to avoid ambiguity when same serial appears on multiple pools
		const deviceModels = await Promise.all(devices.map((d) => this.connectToDevice(`${d._serverKey}/${d.serial}`)));
		// Populate displays/encoders
		await Promise.all(deviceModels.map(async (d) => {
			const [displays, encoders] = await Promise.all([ this.getDeviceDisplays(d.adb), this.getDeviceEncoders(d.adb) ]);
			d.displays = displays; d.encoders = encoders;
		}));
		global.metainfo.features = features; global.metainfo.devices = deviceModels;
		return { version: global.metainfo.version, features: global.metainfo.features, devices: global.metainfo.devices.map((d) => ({ serial: d.serial, name: d.name, displayName: d.displayName, serverKey: d._serverKey, serverHost: d._serverHost, serverPort: d._serverPort, displays: d.displays, encoders: d.encoders })) };
	}

	// Public: refresh the configured pools (recreate connections to ADB servers)
	async refreshPools(serversOverride) {
		try {
			logger.info('Refreshing ADB server pools (manual trigger)');
			const newPools = await createPoolsOnce(serversOverride);
			if (!newPools || newPools.length === 0) {
				logger.warn('refreshPools: no pools could be created during refresh; keeping existing pools');
				return { ok: false, message: 'no-pools-found', count: 0 };
			}
			// Close old connectors/clients that are not in newPools
			const newKeys = new Set(newPools.map(p => p.key));
			for (const old of pools) {
				if (!newKeys.has(old.key)) {
					try {
						if (old.connector && typeof old.connector.close === 'function') old.connector.close();
					} catch (e) { logger.debug(`refreshPools: failed to close old connector ${old.key}: ${e?.message || e}`); }
					try {
						if (old.client && typeof old.client.close === 'function') old.client.close();
					} catch (e) { logger.debug(`refreshPools: failed to close old client ${old.key}: ${e?.message || e}`); }
				}
			}
			// replace pools and update defaultPool
			pools = newPools;
			defaultPool = pools[0];
			logger.info(`refreshPools: replaced pools, new count=${pools.length}`);
			return { ok: true, count: pools.length, keys: pools.map(p => p.key) };
		} catch (e) {
			logger.error('refreshPools: unexpected error', e?.message || e);
			return { ok: false, error: String(e) };
		}
	}
}

export const service = new AdbTcpService();

