/**
 * Push-server utilities for the ADB streaming service.
 *
 * Provides:
 *   - probeRemoteServer: check whether the scrcpy server JAR already exists on
 *     the device at one of the given paths and return its file size.
 *   - pushServerWithCLI: push the server JAR to the device using the `adb push`
 *     CLI command as a fallback when the JavaScript ADB sync API fails.
 *
 * All heavy dependencies are injectable so these functions are fully
 * unit-testable without a real ADB connection or real filesystem.
 */

import { spawn as nodeSpawn } from 'node:child_process';
import { tmpdir as osTmpdir } from 'node:os';
import { join } from 'node:path';
import fs from 'node:fs/promises';
import { v4 as uuid } from 'uuid';

// ─── probeRemoteServer ────────────────────────────────────────────────────────

/**
 * Probe the device for the scrcpy server JAR at each of the given paths.
 *
 * Uses `adb shell ls -l <path>` (via the ADB subprocess API) to determine
 * whether the file exists and what size it has on the device.  The first
 * path where the file is found is returned together with the parsed file size.
 *
 * Returns `{ exists: null, size: null, path: null }` when the subprocess API
 * is unavailable on the current transport (some ADB transport types do not
 * expose it).  Callers should treat this as "inconclusive" and not make
 * decisions based on it.
 *
 * @param {object}    adbInstance - ADB instance; may have `subprocess.exec`.
 * @param {string[]}  tryPaths    - Ordered list of device paths to probe.
 * @param {object}    [logger]    - Optional logger with .info/.debug/.error.
 * @returns {Promise<{ exists: boolean|null, size: number|null, path: string|null }>}
 */
export async function probeRemoteServer(adbInstance, tryPaths, logger = null) {
    const log = logger ?? { info: () => {}, debug: () => {}, error: () => {}, warn: () => {} };
    try {
        if (!adbInstance || !adbInstance.subprocess || typeof adbInstance.subprocess.exec !== 'function') {
            return { exists: null, size: null, path: null };
        }
        for (const p of tryPaths) {
            try {
                const out = await adbInstance.subprocess.exec(['ls', '-l', p]);
                const outStr = Array.isArray(out) ? out.join('\n') : String(out);
                if (/No such file|No such file or directory/i.test(outStr)) continue;
                const lines = outStr.split(/\r?\n/).filter(Boolean);
                if (!lines.length) return { exists: null, size: null, path: p };
                const first = lines[0];
                // Standard `ls -l` output: permissions links user group size date time name
                const m = first.match(/^\S+\s+\d+\s+\S+\s+\S+\s+(\d+)/);
                if (m && m[1]) return { exists: true, size: Number(m[1]), path: p };
                // Fallback: find the first numeric-only token (size)
                const tokens = first.split(/\s+/);
                for (const t of tokens) {
                    if (/^\d+$/.test(t)) return { exists: true, size: Number(t), path: p };
                }
                log.info('probeRemoteServer: could not parse ls -l output for size');
                return { exists: true, size: null, path: p };
            } catch (e) {
                log.debug(`probeRemoteServer: ls -l ${p} failed: ${e?.message || e}`);
            }
        }
        return { exists: false, size: null, path: null };
    } catch (e) {
        log.error(`probeRemoteServer failed: ${e?.message || e}`);
        return { exists: null, size: null, path: null };
    }
}

// ─── pushServerWithCLI ────────────────────────────────────────────────────────

/**
 * Push the scrcpy server binary to the device using the `adb push` CLI command.
 *
 * This is intended as a last-resort fallback when the JavaScript ADB sync API
 * (`AdbScrcpyClient.pushServer`) fails.  The binary is written to a temporary
 * file on the host so that the `adb` command-line tool can read it from disk.
 * The temporary file is always removed when the function returns, regardless of
 * whether the push succeeded or failed.
 *
 * @param {string|null}       serial       - ADB device serial (passed as `-s <serial>`),
 *                                           or `null` / `undefined` for the default device.
 * @param {Buffer|Uint8Array} serverBuffer - Raw bytes of the scrcpy server JAR.
 * @param {string}            targetPath   - Destination path on the device
 *                                           (e.g. "/data/local/tmp/scrcpy-server.jar").
 * @param {object}            [opts]       - Injectable dependencies for testability.
 * @param {Function}          [opts.spawnFn]       - Replaces `child_process.spawn`.
 * @param {Function}          [opts.writeTempFile] - Replaces `fs.writeFile(path, data)`.
 * @param {Function}          [opts.removeTempFile] - Replaces `fs.unlink(path)`.
 * @param {Function}          [opts.getTmpdir]     - Replaces `os.tmpdir()` (called as a fn).
 * @param {object}            [opts.logger]        - Logger instance.
 * @returns {Promise<void>}
 */
export async function pushServerWithCLI(serial, serverBuffer, targetPath, {
    spawnFn = nodeSpawn,
    writeTempFile = async (path, data) => fs.writeFile(path, data),
    removeTempFile = async (path) => fs.unlink(path),
    getTmpdir = osTmpdir,
    logger = null,
} = {}) {
    const log = logger ?? { info: () => {}, debug: () => {}, error: () => {}, warn: () => {} };
    const tempPath = join(getTmpdir(), `scrcpy-server-${uuid()}.jar`);
    try {
        const byteLen = serverBuffer?.byteLength ?? serverBuffer?.length ?? 0;
        log.debug(`pushServerWithCLI: writing server binary (${byteLen} bytes) to temp file ${tempPath}`);
        await writeTempFile(tempPath, serverBuffer);

        const args = serial
            ? ['-s', serial, 'push', tempPath, targetPath]
            : ['push', tempPath, targetPath];
        log.info(`pushServerWithCLI: running: adb ${args.join(' ')}`);

        await new Promise((resolve, reject) => {
            let child;
            try {
                child = spawnFn('adb', args);
            } catch (spawnErr) {
                reject(spawnErr);
                return;
            }
            const stdoutLines = [];
            const stderrLines = [];
            child.stdout?.on('data', (data) => stdoutLines.push(data.toString()));
            child.stderr?.on('data', (data) => {
                const msg = data.toString();
                stderrLines.push(msg);
                log.debug(`pushServerWithCLI stderr: ${msg.trim()}`);
            });
            child.on('error', (err) => reject(err));
            child.on('exit', (code) => {
                if (code === 0) {
                    const out = stdoutLines.join('').trim();
                    log.info(`pushServerWithCLI: adb push succeeded${out ? ` — ${out}` : ''}`);
                    resolve();
                } else {
                    reject(new Error(`adb push exited with code ${code}: ${stderrLines.join('')}`));
                }
            });
        });
    } finally {
        try {
            await removeTempFile(tempPath);
        } catch (e) {
            log.debug(`pushServerWithCLI: failed to clean up temp file ${tempPath}: ${e?.message || e}`);
        }
    }
}
