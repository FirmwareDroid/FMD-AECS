/**
 * Unit tests for the ADB app-install functionality.
 *
 * Because adb-tcp-service.js contains module-level awaits that require a live
 * ADB server, we test the install logic in isolation by:
 *   1. Testing the `getAppPath` path-building utility directly.
 *   2. Exercising the controller's input-validation branch through a thin
 *      simulation that mirrors the controller's logic without importing the
 *      real ADB service.
 *
 * Run with: node --test source/tests/adb-install.test.js
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { join } from 'node:path';

import { getAppPath, UPLOAD_DIR, APPS_DIR } from '../services/adb/getAppPath.js';

// ---------------------------------------------------------------------------
// getAppPath
// ---------------------------------------------------------------------------

describe('getAppPath', () => {
    test('returns path inside UPLOAD_DIR when source is "uploads"', () => {
        const result = getAppPath('uploads', 'test.apk');
        assert.equal(result, join(UPLOAD_DIR, 'test.apk'));
    });

    test('returns path inside APPS_DIR when source is anything else', () => {
        const result = getAppPath('apps', 'test.apk');
        assert.equal(result, join(APPS_DIR, 'test.apk'));
    });

    test('returns path inside APPS_DIR when source is undefined', () => {
        const result = getAppPath(undefined, 'app.apk');
        assert.equal(result, join(APPS_DIR, 'app.apk'));
    });

    test('includes the app filename verbatim', () => {
        const filename = 'my_app_1.0.0.apk';
        const result = getAppPath('uploads', filename);
        assert.ok(result.endsWith(filename), `Expected path to end with "${filename}"`);
    });
});

// ---------------------------------------------------------------------------
// installApp controller — input validation (simulated, no ADB service needed)
// ---------------------------------------------------------------------------

/**
 * Simulate the input-validation part of `AdbController.installApp` without
 * importing the real controller (which depends on adb-tcp-service with
 * module-level side effects).
 */
function simulateInstallAppHandler({ app: rawApp, device, source = 'uploads' }) {
    const responses = [];

    const res = {
        status: null,
        body: null,
        setStatus(s) { this.status = s; return this; },
        send(b) { this.body = b; responses.push({ status: this.status, body: b }); },
    };

    // Mirror the validation logic from adb-controller.js → installApp
    if (!rawApp) {
        res.setStatus('400').send({ ok: false, error: 'Missing required query parameter: app' });
        return responses[0];
    }
    if (!device) {
        res.setStatus('400').send({ ok: false, error: 'Missing required query parameter: device' });
        return responses[0];
    }

    const { basename } = { basename: (p) => p.replace(/.*[/\\]/, '') };
    const app = basename(rawApp.replace(/\0/g, ''));
    if (!app || /^\.+$/.test(app)) {
        res.setStatus('400').send({ ok: false, error: 'Invalid app filename' });
        return responses[0];
    }
    const ext = app.split('.').pop()?.toLowerCase();
    const allowed = new Set(['apk', 'apkm', 'xapk']);
    if (!ext || !allowed.has(ext)) {
        res.setStatus('400').send({ ok: false, error: `Disallowed file extension: .${ext}. Allowed: ${[...allowed].join(', ')}` });
        return responses[0];
    }

    // If all validation passes, pretend the service call succeeded
    res.send({ ok: true, output: 'Success' });
    return responses[0];
}

describe('installApp controller — input validation', () => {
    test('returns 400 when "app" query parameter is missing', () => {
        const response = simulateInstallAppHandler({ app: null, device: 'emulator-5554' });
        assert.equal(response.status, '400');
        assert.equal(response.body.ok, false);
        assert.ok(response.body.error.includes('app'));
    });

    test('returns 400 when "device" query parameter is missing', () => {
        const response = simulateInstallAppHandler({ app: 'test.apk', device: null });
        assert.equal(response.status, '400');
        assert.equal(response.body.ok, false);
        assert.ok(response.body.error.includes('device'));
    });

    test('returns 400 with empty string for "app"', () => {
        const response = simulateInstallAppHandler({ app: '', device: 'emulator-5554' });
        assert.equal(response.status, '400');
        assert.equal(response.body.ok, false);
    });

    test('returns 400 with empty string for "device"', () => {
        const response = simulateInstallAppHandler({ app: 'test.apk', device: '' });
        assert.equal(response.status, '400');
        assert.equal(response.body.ok, false);
    });

    test('passes validation when both "app" and "device" are provided', () => {
        const response = simulateInstallAppHandler({ app: 'test.apk', device: 'emulator-5554' });
        assert.equal(response.status, null, 'status should be null (200 OK) on success');
        assert.equal(response.body.ok, true);
    });

    test('returns 400 for disallowed file extension (.exe)', () => {
        const response = simulateInstallAppHandler({ app: 'malware.exe', device: 'emulator-5554' });
        assert.equal(response.status, '400');
        assert.ok(response.body.error.includes('Disallowed'));
    });

    test('strips path traversal from app filename', () => {
        // "../../../etc/passwd" should be stripped to "passwd" which has no valid extension
        const response = simulateInstallAppHandler({ app: '../../../etc/passwd', device: 'emulator-5554' });
        assert.equal(response.status, '400');
    });

    test('accepts .apkm extension', () => {
        const response = simulateInstallAppHandler({ app: 'bundle.apkm', device: 'emulator-5554' });
        assert.equal(response.status, null);
        assert.equal(response.body.ok, true);
    });

    test('accepts .xapk extension', () => {
        const response = simulateInstallAppHandler({ app: 'bundle.xapk', device: 'emulator-5554' });
        assert.equal(response.status, null);
        assert.equal(response.body.ok, true);
    });
});

// ---------------------------------------------------------------------------
// installApk service method — isolated logic tests
// ---------------------------------------------------------------------------

/**
 * Create a minimal mock that reproduces the core logic of AdbTcpService.installApk
 * without requiring a live ADB connection.
 */
function makeInstallApkStub({ getDeviceAdbResult, pmResult, fsStatResult, fsOpenResult } = {}) {
    const calls = { getDeviceAdb: [], pmPushAndInstall: [], fsOpen: [], fsStat: [] };
    // Distinguish "not provided" from explicitly-provided null/undefined
    const pmResultProvided = arguments[0] != null && Object.prototype.hasOwnProperty.call(arguments[0], 'pmResult');

    const mockFs = {
        stat: async (path) => {
            calls.fsStat.push(path);
            if (fsStatResult instanceof Error) throw fsStatResult;
            return fsStatResult ?? { size: 1024 };
        },
        open: async (path) => {
            calls.fsOpen.push(path);
            if (fsOpenResult instanceof Error) throw fsOpenResult;
            return {
                readableWebStream: null,
                close: async () => {},
            };
        },
        readFile: async () => Buffer.alloc(0),
    };

    const mockGetDeviceAdb = async (deviceParam) => {
        calls.getDeviceAdb.push(deviceParam);
        if (getDeviceAdbResult instanceof Error) throw getDeviceAdbResult;
        return getDeviceAdbResult ?? {};
    };

    const mockPm = {
        pushAndInstallStream: async () => {
            calls.pmPushAndInstall.push(true);
            if (pmResult instanceof Error) throw pmResult;
            // Return the caller-supplied value (may be null) if explicitly set,
            // otherwise fall back to a sensible default.
            return pmResultProvided ? pmResult : 'Success';
        },
    };

    // Replicate installApk logic
    const installApk = async (deviceParam, apkPath, options = {}) => {
        const deviceAdb = await mockGetDeviceAdb(deviceParam);
        void deviceAdb; // used to create PackageManager in real code

        const stat = await mockFs.stat(apkPath);
        const fileSize = stat.size;
        void fileSize;

        const fileHandle = await mockFs.open(apkPath, 'r');
        try {
            const fileStream = new ReadableStream({
                async start(controller) {
                    const buf = await mockFs.readFile(apkPath);
                    controller.enqueue(buf);
                    controller.close();
                },
            });
            const output = await mockPm.pushAndInstallStream(fileStream, options);
            return { ok: true, output: output ?? '' };
        } finally {
            await fileHandle.close();
        }
    };

    return { installApk, calls };
}

describe('installApk service — isolated logic', () => {
    test('returns { ok: true, output } on success', async () => {
        const { installApk } = makeInstallApkStub({ pmResult: 'Success' });
        const result = await installApk('emulator-5554', '/tmp/test.apk');
        assert.equal(result.ok, true);
        assert.equal(result.output, 'Success');
    });

    test('output defaults to empty string when pm returns null', async () => {
        const { installApk } = makeInstallApkStub({ pmResult: null });
        const result = await installApk('emulator-5554', '/tmp/test.apk');
        assert.equal(result.ok, true);
        assert.equal(result.output, '');
    });

    test('propagates error from getDeviceAdb', async () => {
        const { installApk } = makeInstallApkStub({
            getDeviceAdbResult: new Error('Device not connected'),
        });
        await assert.rejects(
            () => installApk('missing-device', '/tmp/test.apk'),
            { message: 'Device not connected' },
        );
    });

    test('propagates error from fs.stat (file not found)', async () => {
        const { installApk } = makeInstallApkStub({
            fsStatResult: Object.assign(new Error('ENOENT'), { code: 'ENOENT' }),
        });
        await assert.rejects(
            () => installApk('emulator-5554', '/tmp/missing.apk'),
            { message: 'ENOENT' },
        );
    });

    test('propagates error thrown by PackageManager.pushAndInstallStream', async () => {
        const { installApk } = makeInstallApkStub({
            pmResult: new Error('Install failed: INSTALL_FAILED_UPDATE_INCOMPATIBLE'),
        });
        await assert.rejects(
            () => installApk('emulator-5554', '/tmp/test.apk'),
            { message: 'Install failed: INSTALL_FAILED_UPDATE_INCOMPATIBLE' },
        );
    });

    test('stat is called with the provided apkPath', async () => {
        const { installApk, calls } = makeInstallApkStub();
        await installApk('emulator-5554', '/uploads/my_app.apk');
        assert.equal(calls.fsStat[0], '/uploads/my_app.apk');
    });

    test('getDeviceAdb is called with the provided device identifier', async () => {
        const { installApk, calls } = makeInstallApkStub();
        await installApk('localhost:5037/emulator-5554', '/uploads/my_app.apk');
        assert.equal(calls.getDeviceAdb[0], 'localhost:5037/emulator-5554');
    });
});
