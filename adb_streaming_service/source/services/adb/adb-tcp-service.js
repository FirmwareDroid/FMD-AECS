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
const server = userProvidedLocalServerPath
	? await (async () => {
		try {
			await fs.access(LOCAL_SERVER_PATH_RESOLVED);
			logger.info(`Using local DEFAULT_SERVER_PATH: ${LOCAL_SERVER_PATH_RESOLVED}`);
			return await fs.readFile(LOCAL_SERVER_PATH_RESOLVED);
		} catch (err) {
			logger.error(`DEFAULT_SERVER_PATH not found or not accessible: ${LOCAL_SERVER_PATH_RESOLVED}`);
			logger.error("Please set DEFAULT_SERVER_PATH to the local path of the scrcpy server binary (accessible by the service) or remove the env var to use the bundled server.");
			process.exit(1);
		}
	})()
	: await fs.readFile(BIN);

// Device-side server path (where the server will be pushed on the device).
// Allow override via SCRCPY_DEVICE_PATH env; otherwise use DefaultServerPath.
const DEVICE_SERVER_PATH = process.env.SCRCPY_DEVICE_PATH || DefaultServerPath;

const connector = new AdbServerNodeTcpConnector({ host: "localhost", port: 5037 });
const serverClient = new AdbServerClient(connector);

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
		logger.debug('resolveSerialFromAdbInstance error:', e?.message || e);
	}
	return null;
}

const pushServer = async (adbInstance, force = false) => {
	const serial = resolveSerialFromAdbInstance(adbInstance);
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
			return { exists: true, size: null };
		} catch (e) {
			logger.debug('probeRemoteServer failed:', e?.message || e);
			return { exists: null, size: null };
		}
	};

	if (serial) {
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
	async getFeatures() { return await serverClient.getServerFeatures(); }
	async getDevices() { return await serverClient.getDevices(); }

	async connectToDevice(serial) {
		const transport = await serverClient.createTransport({ serial });
		const adb = new Adb(transport);
		return { serial, transport, adb, displays: [], encoders: [] };
	}

	async getDeviceDisplays(deviceAdb) {
		let result = [];
		let trial = 0;
		// push server once before trials
		try { await pushServer(deviceAdb); } catch (err) { logger.error('Initial pushServer failed in getDeviceDisplays:', err); }
		// optional: try to verify file exists on device (best-effort)
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

	async start(deviceAdb, user) {
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
		if (audioEncoder) init.audioEncoder = audioEncoder;
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

		const options = new AdbScrcpyOptions2_1(init, { version: VERSION });

		// ensure server binary is present on the device
		await pushServer(deviceAdb);
		try {
			const client = await AdbScrcpyClient.start(deviceAdb, DEVICE_SERVER_PATH, options);
			return { client, options };
		} catch (err) {
			const details = { message: err && err.message ? err.message : String(err), name: err && err.name ? err.name : 'Error', stack: err && err.stack ? err.stack : null, server_length: server ? (server.byteLength || server.length || null) : null, logcat: null, device_ls: null };
			try {
				if (deviceAdb && deviceAdb.subprocess && typeof deviceAdb.subprocess.exec === 'function') {
					details.logcat = await deviceAdb.subprocess.exec(["logcat", "-d", "-t", "200"]);
				}
			} catch (e) {
				logger.debug('Failed to collect logcat during scrcpy start error:', e?.message || e);
			}
			// Try to inspect server file on device
			try {
				if (deviceAdb && deviceAdb.subprocess && typeof deviceAdb.subprocess.exec === 'function') {
					details.device_ls = await deviceAdb.subprocess.exec(["ls", "-l", DEVICE_SERVER_PATH]);
				}
			} catch (e) {
				logger.debug('Failed to run ls on device for server file:', e?.message || e);
			}
			const e2 = new Error(`scrcpy start failed: ${details.message}`); e2.details = details; throw e2;
		}
	}

	async getDeviceAdb(deviceSerial) {
		let device = global.metainfo.devices.find((d) => d.serial === deviceSerial);
		if (!device) { await this.metainfo(); }
		device = global.metainfo.devices.find((d) => d.serial === deviceSerial);
		if (!device) { throw new Error(`Device with serial = '${deviceSerial}' is not connected.`); }
		return device.adb;
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
		const deviceModels = await Promise.all(devices.map((d) => this.connectToDevice(d.serial)));
		await Promise.all(deviceModels.map(async (d) => {
			const [displays, encoders] = await Promise.all([ this.getDeviceDisplays(d.adb), this.getDeviceEncoders(d.adb) ]);
			d.displays = displays; d.encoders = encoders;
		}));
		global.metainfo.features = features; global.metainfo.devices = deviceModels;
		return { version: global.metainfo.version, features: global.metainfo.features, devices: global.metainfo.devices.map((d) => ({ serial: d.serial, displays: d.displays, encoders: d.encoders })) };
	}

}

export const service = new AdbTcpService();
