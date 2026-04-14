import { service as adbShellService } from "../../services/adb/adb-shell-service.js";
import { service as adbTcpService } from "../../services/adb/adb-tcp-service.js";
import { logger } from "../../services/logger.js";
import { getAppPath } from "../../services/adb/getAppPath.js";
import { basename } from "node:path";

const ALLOWED_INSTALL_EXTENSIONS = new Set(['apk', 'apkm', 'xapk']);

class AdbController {
	async metainfo(res, _req) {
		try {
			const result = await adbTcpService.metainfo();
			res.send(result);
		} catch (ex) {
			logger.error(ex);
			res.setStatus("500").send(ex);
		}
	}

	async install(res, req) {
		const app = req.getQuery("app") ?? "stashcat_6893305.apk";
		const device = req.getQuery("device") ?? "localhost:6666";
		const source = req.getQuery("source") ?? "uploads";

		try {
			let result
			if (app.toLowerCase().endsWith('apkm') || app.toLowerCase().endsWith('xapk')) {
				result = await adbShellService.installMultiple(app, device, source);
			} else {
				result = await adbShellService.install(app, device, source);
			}
			res.send(result);
		} catch (ex) {
			logger.error(ex);
			res.setStatus("500").send(ex);
		}
	}

	async start(res, req) {
		const app = req.getQuery("app") ?? "stashcat_6893305.apk";
		const device = req.getQuery("device") ?? "localhost:6666";
		const source = req.getQuery("source") ?? "uploads";

		try {
			const result = await adbShellService.start(app, device, source);
			res.send(result);
		} catch (ex) {
			logger.error(ex);
			res.setStatus("500").send(ex);
		}
	}

	async pin(res, req) {
		const app = req.getQuery("app") ?? "stashcat_6893305.apk";
		const device = req.getQuery("device") ?? "localhost:6666";
		const source = req.getQuery("source") ?? "uploads";

		try {
			const result = await adbShellService.pin(app, device, source);
			res.send(result);
		} catch (ex) {
			logger.error(ex);
			res.setStatus("500").send(ex);
		}
	}

	async unpin(res, req) {
		const app = req.getQuery("app") ?? "stashcat_6893305.apk";
		const device = req.getQuery("device") ?? "localhost:6666";

		try {
			const result = await adbShellService.unpin(app, device);
			res.send(result);
		} catch (ex) {
			logger.error(ex);
			res.setStatus("500").send(ex);
		}
	}

	// Manual refresh of configured ADB server pools (reconnect to ADB servers)
	async refreshPools(res, req) {
		try {
			const servers = req.getQuery('servers') ?? null; // comma-separated host:port entries (optional)
			const result = await adbTcpService.refreshPools(servers);
			res.send(result);
		} catch (ex) {
			logger.error('refreshPools handler error', ex);
			res.setStatus('500').send({ ok: false, error: String(ex) });
		}
	}

	/**
	 * Install an APK from the uploads or apps directory onto a specific ADB
	 * device using the ADB protocol (no shell invocation).
	 *
	 * Query parameters:
	 *   - app    {string} filename of the APK (required)
	 *   - device {string} device serial or unique name host:port/serial (required)
	 *   - source {string} "uploads" (default) or "apps"
	 */
	async installApp(res, req) {
		const rawApp = req.getQuery('app') ?? null;
		const device = req.getQuery('device') ?? null;
		const source = req.getQuery('source') ?? 'uploads';

		if (!rawApp) {
			res.setStatus('400').send({ ok: false, error: 'Missing required query parameter: app' });
			return;
		}
		if (!device) {
			res.setStatus('400').send({ ok: false, error: 'Missing required query parameter: device' });
			return;
		}

		// Sanitize the filename: strip directory components and null bytes, then
		// validate the extension to prevent path traversal and unexpected file types.
		const app = basename(rawApp.replace(/\0/g, ''));
		if (!app || /^\.+$/.test(app)) {
			res.setStatus('400').send({ ok: false, error: 'Invalid app filename' });
			return;
		}
		const ext = app.split('.').pop()?.toLowerCase();
		if (!ext || !ALLOWED_INSTALL_EXTENSIONS.has(ext)) {
			res.setStatus('400').send({ ok: false, error: `Disallowed file extension: .${ext}. Allowed: ${[...ALLOWED_INSTALL_EXTENSIONS].join(', ')}` });
			return;
		}

		const apkPath = getAppPath(source, app);
		try {
			const result = await adbTcpService.installApk(device, apkPath);
			res.send(result);
		} catch (ex) {
			logger.error('installApp handler error', ex);
			res.setStatus('500').send({ ok: false, error: String(ex) });
		}
	}
}

export const controller = new AdbController();
