/**
 * Unit tests for source/services/adb/push-server.js
 *
 * Tests cover:
 *   - probeRemoteServer: binary existence detection, size parsing, path fallback
 *   - pushServerWithCLI: temp-file lifecycle, adb invocation, error handling
 *
 * Run with: node --test source/tests/push-server.test.js
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { probeRemoteServer, pushServerWithCLI } from '../services/adb/push-server.js';

// ─── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Build a minimal ADB instance mock.
 *
 * @param {Function} execFn  Called with the args array passed to subprocess.exec.
 *                           Return a string or throw to simulate device output.
 */
function makeAdb(execFn) {
    return {
        subprocess: {
            exec: execFn,
        },
    };
}

/**
 * Build a mock child_process.spawn that fires stdout/stderr data and an exit
 * event asynchronously (after all listeners have been registered).
 *
 * @param {object} opts
 * @param {number} [opts.exitCode=0]
 * @param {string} [opts.stdoutData='']
 * @param {string} [opts.stderrData='']
 * @param {Error}  [opts.spawnError]   - If set, the constructor throws instead.
 * @returns {{ spawn: Function, calls: Array }}
 */
function makeMockSpawn({ exitCode = 0, stdoutData = '', stderrData = '', spawnError = null } = {}) {
    const calls = [];

    function spawn(cmd, args) {
        calls.push({ cmd, args: [...args] });

        if (spawnError) throw spawnError;

        const proc = new EventEmitter();
        proc.stdout = new EventEmitter();
        proc.stderr = new EventEmitter();

        setImmediate(() => {
            if (stdoutData) proc.stdout.emit('data', stdoutData);
            if (stderrData) proc.stderr.emit('data', stderrData);
            proc.emit('exit', exitCode);
        });

        return proc;
    }

    spawn.calls = calls;
    return spawn;
}

// ─── probeRemoteServer ────────────────────────────────────────────────────────

describe('probeRemoteServer — subprocess unavailable', () => {
    test('returns { exists: null } when adbInstance is null', async () => {
        const result = await probeRemoteServer(null, ['/data/local/tmp/server.jar']);
        assert.deepEqual(result, { exists: null, size: null, path: null });
    });

    test('returns { exists: null } when adbInstance has no subprocess', async () => {
        const result = await probeRemoteServer({}, ['/data/local/tmp/server.jar']);
        assert.deepEqual(result, { exists: null, size: null, path: null });
    });

    test('returns { exists: null } when subprocess.exec is not a function', async () => {
        const adb = { subprocess: { exec: 'not-a-function' } };
        const result = await probeRemoteServer(adb, ['/data/local/tmp/server.jar']);
        assert.deepEqual(result, { exists: null, size: null, path: null });
    });
});

describe('probeRemoteServer — binary found', () => {
    test('parses standard ls -l output and returns exists=true with size', async () => {
        const lsOutput = '-rw-rw-rw- 1 shell shell 98765 2024-01-01 10:00 /data/local/tmp/server.jar';
        const adb = makeAdb(async () => lsOutput);
        const result = await probeRemoteServer(adb, ['/data/local/tmp/server.jar']);
        assert.equal(result.exists, true);
        assert.equal(result.size, 98765);
        assert.equal(result.path, '/data/local/tmp/server.jar');
    });

    test('returns the first matching path when multiple paths are given', async () => {
        const lsOutput = '-rw-rw-rw- 1 shell shell 12345 2024-01-01 10:00 /data/local/tmp/server.jar';
        // exec always succeeds regardless of path
        const adb = makeAdb(async () => lsOutput);
        const result = await probeRemoteServer(adb, [
            '/data/local/tmp/server.jar',
            '/sdcard/Download/server.jar',
        ]);
        assert.equal(result.path, '/data/local/tmp/server.jar');
    });

    test('falls back to second path when first reports "No such file"', async () => {
        const execFn = async (args) => {
            if (args.includes('/data/local/tmp/server.jar')) {
                return 'ls: /data/local/tmp/server.jar: No such file or directory';
            }
            return '-rw-rw-rw- 1 shell shell 42000 2024-01-01 10:00 /sdcard/Download/server.jar';
        };
        const adb = makeAdb(execFn);
        const result = await probeRemoteServer(adb, [
            '/data/local/tmp/server.jar',
            '/sdcard/Download/server.jar',
        ]);
        assert.equal(result.exists, true);
        assert.equal(result.size, 42000);
        assert.equal(result.path, '/sdcard/Download/server.jar');
    });

    test('falls back to second path when exec throws for the first path', async () => {
        const execFn = async (args) => {
            if (args.includes('/data/local/tmp/server.jar')) {
                throw new Error('permission denied');
            }
            return '-rw-rw-rw- 1 shell shell 55000 2024-01-01 10:00 /sdcard/Download/server.jar';
        };
        const adb = makeAdb(execFn);
        const result = await probeRemoteServer(adb, [
            '/data/local/tmp/server.jar',
            '/sdcard/Download/server.jar',
        ]);
        assert.equal(result.exists, true);
        assert.equal(result.path, '/sdcard/Download/server.jar');
    });

    test('returns { exists: true, size: null } when ls output cannot be parsed for size', async () => {
        // No numeric size token in this (contrived) output
        const adb = makeAdb(async () => 'some-file-info-without-size /path/to/file');
        const result = await probeRemoteServer(adb, ['/data/local/tmp/server.jar']);
        assert.equal(result.exists, true);
        assert.equal(result.size, null);
    });

    test('returns { exists: null, size: null } when ls returns no lines', async () => {
        const adb = makeAdb(async () => '');
        const result = await probeRemoteServer(adb, ['/data/local/tmp/server.jar']);
        assert.deepEqual(result, { exists: null, size: null, path: '/data/local/tmp/server.jar' });
    });

    test('falls back to numeric token when standard regex does not match', async () => {
        // Compact ls output seen on some BusyBox builds
        const adb = makeAdb(async () => '-rw-r--r-- shell 77300 server.jar');
        const result = await probeRemoteServer(adb, ['/data/local/tmp/server.jar']);
        assert.equal(result.exists, true);
        assert.equal(result.size, 77300);
    });
});

describe('probeRemoteServer — binary not found', () => {
    test('returns { exists: false } when all paths report "No such file"', async () => {
        const adb = makeAdb(async () => 'ls: /some/path: No such file or directory');
        const result = await probeRemoteServer(adb, [
            '/data/local/tmp/server.jar',
            '/sdcard/Download/server.jar',
        ]);
        assert.deepEqual(result, { exists: false, size: null, path: null });
    });

    test('returns { exists: false } with an empty path list', async () => {
        const adb = makeAdb(async () => { throw new Error('should not be called'); });
        const result = await probeRemoteServer(adb, []);
        assert.deepEqual(result, { exists: false, size: null, path: null });
    });

    test('returns { exists: false } when all exec calls throw', async () => {
        const adb = makeAdb(async () => { throw new Error('device offline'); });
        const result = await probeRemoteServer(adb, [
            '/data/local/tmp/server.jar',
            '/sdcard/Download/server.jar',
        ]);
        assert.deepEqual(result, { exists: false, size: null, path: null });
    });
});

describe('probeRemoteServer — logger integration', () => {
    test('logs parse failure when ls output has no numeric token', async () => {
        const logMessages = [];
        const log = {
            info:  (...a) => logMessages.push(['info',  a.join(' ')]),
            debug: (...a) => logMessages.push(['debug', a.join(' ')]),
            error: (...a) => logMessages.push(['error', a.join(' ')]),
            warn:  (...a) => logMessages.push(['warn',  a.join(' ')]),
        };
        const adb = makeAdb(async () => 'some-file-info-without-size /path/to/file');
        await probeRemoteServer(adb, ['/data/local/tmp/server.jar'], log);
        const infoMsgs = logMessages.filter(([level]) => level === 'info').map(([, msg]) => msg);
        assert.ok(
            infoMsgs.some((m) => m.includes('could not parse')),
            'expected a log message about unparseable output'
        );
    });
});

// ─── pushServerWithCLI ────────────────────────────────────────────────────────

describe('pushServerWithCLI — successful push', () => {
    test('writes server binary to a temp file before invoking adb', async () => {
        const written = [];
        const mockSpawn = makeMockSpawn({ exitCode: 0 });

        await pushServerWithCLI('emulator-5554', Buffer.from('binary-data'), '/data/local/tmp/server.jar', {
            spawnFn: mockSpawn,
            writeTempFile: async (path, data) => { written.push({ path, data }); },
            removeTempFile: async () => {},
            getTmpdir: () => '/tmp',
        });

        assert.equal(written.length, 1, 'expected exactly one temp-file write');
        assert.equal(String(written[0].data), 'binary-data');
    });

    test('passes the temp-file path and device serial to adb push', async () => {
        const mockSpawn = makeMockSpawn({ exitCode: 0 });
        let tempPath;

        await pushServerWithCLI('emulator-5554', Buffer.from('data'), '/data/local/tmp/server.jar', {
            spawnFn: mockSpawn,
            writeTempFile: async (path) => { tempPath = path; },
            removeTempFile: async () => {},
            getTmpdir: () => '/tmp',
        });

        assert.equal(mockSpawn.calls.length, 1);
        const { cmd, args } = mockSpawn.calls[0];
        assert.equal(cmd, 'adb');
        assert.deepEqual(args, ['-s', 'emulator-5554', 'push', tempPath, '/data/local/tmp/server.jar']);
    });

    test('omits -s flag when serial is null', async () => {
        const mockSpawn = makeMockSpawn({ exitCode: 0 });

        await pushServerWithCLI(null, Buffer.from('data'), '/data/local/tmp/server.jar', {
            spawnFn: mockSpawn,
            writeTempFile: async () => {},
            removeTempFile: async () => {},
            getTmpdir: () => '/tmp',
        });

        const { args } = mockSpawn.calls[0];
        assert.ok(!args.includes('-s'), 'expected no -s flag when serial is null');
        assert.equal(args[0], 'push');
    });

    test('omits -s flag when serial is undefined', async () => {
        const mockSpawn = makeMockSpawn({ exitCode: 0 });

        await pushServerWithCLI(undefined, Buffer.from('data'), '/data/local/tmp/server.jar', {
            spawnFn: mockSpawn,
            writeTempFile: async () => {},
            removeTempFile: async () => {},
            getTmpdir: () => '/tmp',
        });

        const { args } = mockSpawn.calls[0];
        assert.ok(!args.includes('-s'), 'expected no -s flag when serial is undefined');
    });

    test('removes the temp file after a successful push', async () => {
        const removed = [];
        const mockSpawn = makeMockSpawn({ exitCode: 0 });

        await pushServerWithCLI('device-1', Buffer.from('data'), '/data/local/tmp/server.jar', {
            spawnFn: mockSpawn,
            writeTempFile: async (path) => {},
            removeTempFile: async (path) => { removed.push(path); },
            getTmpdir: () => '/tmp',
        });

        assert.equal(removed.length, 1, 'expected temp file to be removed on success');
    });

    test('uses a unique temp filename for each invocation (no collisions)', async () => {
        const tempPaths = [];
        const mockSpawn = makeMockSpawn({ exitCode: 0 });

        for (let i = 0; i < 3; i++) {
            await pushServerWithCLI('device-1', Buffer.from('data'), '/data/local/tmp/server.jar', {
                spawnFn: mockSpawn,
                writeTempFile: async (path) => { tempPaths.push(path); },
                removeTempFile: async () => {},
                getTmpdir: () => '/tmp',
            });
        }

        const uniquePaths = new Set(tempPaths);
        assert.equal(uniquePaths.size, 3, 'expected a unique temp path for each invocation');
    });
});

describe('pushServerWithCLI — failure handling', () => {
    test('throws when adb exits with a non-zero code', async () => {
        const mockSpawn = makeMockSpawn({ exitCode: 1, stderrData: 'device not found' });

        await assert.rejects(
            () => pushServerWithCLI('emulator-5554', Buffer.from('data'), '/data/local/tmp/server.jar', {
                spawnFn: mockSpawn,
                writeTempFile: async () => {},
                removeTempFile: async () => {},
                getTmpdir: () => '/tmp',
            }),
            (err) => {
                assert.ok(err instanceof Error, 'expected an Error');
                assert.ok(err.message.includes('code 1'), `unexpected error message: ${err.message}`);
                return true;
            }
        );
    });

    test('removes the temp file even when adb exits with a non-zero code', async () => {
        const removed = [];
        const mockSpawn = makeMockSpawn({ exitCode: 1 });

        await assert.rejects(
            () => pushServerWithCLI('device-1', Buffer.from('data'), '/data/local/tmp/server.jar', {
                spawnFn: mockSpawn,
                writeTempFile: async () => {},
                removeTempFile: async (path) => { removed.push(path); },
                getTmpdir: () => '/tmp',
            })
        );

        assert.equal(removed.length, 1, 'expected temp file to be removed on failure');
    });

    test('throws when spawn itself throws', async () => {
        const mockSpawn = makeMockSpawn({ spawnError: new Error('ENOENT: adb not found') });

        await assert.rejects(
            () => pushServerWithCLI('device-1', Buffer.from('data'), '/data/local/tmp/server.jar', {
                spawnFn: mockSpawn,
                writeTempFile: async () => {},
                removeTempFile: async () => {},
                getTmpdir: () => '/tmp',
            }),
            /ENOENT/
        );
    });

    test('removes the temp file even when spawn throws', async () => {
        const removed = [];
        const mockSpawn = makeMockSpawn({ spawnError: new Error('spawn failed') });

        await assert.rejects(
            () => pushServerWithCLI('device-1', Buffer.from('data'), '/data/local/tmp/server.jar', {
                spawnFn: mockSpawn,
                writeTempFile: async () => {},
                removeTempFile: async (path) => { removed.push(path); },
                getTmpdir: () => '/tmp',
            })
        );

        assert.equal(removed.length, 1, 'expected temp file to be removed when spawn throws');
    });

    test('removes the temp file even when writeTempFile throws', async () => {
        const removed = [];
        const mockSpawn = makeMockSpawn({ exitCode: 0 });

        await assert.rejects(
            () => pushServerWithCLI('device-1', Buffer.from('data'), '/data/local/tmp/server.jar', {
                spawnFn: mockSpawn,
                writeTempFile: async () => { throw new Error('disk full'); },
                removeTempFile: async (path) => { removed.push(path); },
                getTmpdir: () => '/tmp',
            }),
            /disk full/
        );

        assert.equal(removed.length, 1, 'expected cleanup even when writeTempFile throws');
    });

    test('throws immediately when serverBuffer is null', async () => {
        await assert.rejects(
            () => pushServerWithCLI('device-1', null, '/data/local/tmp/server.jar', {
                spawnFn: makeMockSpawn(),
                writeTempFile: async () => {},
                removeTempFile: async () => {},
                getTmpdir: () => '/tmp',
            }),
            /serverBuffer must be provided/
        );
    });

    test('throws immediately when serverBuffer is undefined', async () => {
        await assert.rejects(
            () => pushServerWithCLI('device-1', undefined, '/data/local/tmp/server.jar', {
                spawnFn: makeMockSpawn(),
                writeTempFile: async () => {},
                removeTempFile: async () => {},
                getTmpdir: () => '/tmp',
            }),
            /serverBuffer must be provided/
        );
    });
});

// ─── Interaction: probe after push ───────────────────────────────────────────

describe('probeRemoteServer — confirms binary existence after a push', () => {
    test('reports the binary as present when ls -l confirms correct size', async () => {
        const EXPECTED_SIZE = 102400;
        // Simulate the device having the binary at the correct path after push
        const adb = makeAdb(async (args) => {
            const path = args[args.length - 1];
            return `-rw-rw-rw- 1 shell shell ${EXPECTED_SIZE} 2024-01-01 10:00 ${path}`;
        });

        const result = await probeRemoteServer(adb, ['/data/local/tmp/server.jar']);

        assert.equal(result.exists, true,  'binary should exist on device');
        assert.equal(result.size, EXPECTED_SIZE, 'reported size should match expected size');
        assert.equal(result.path, '/data/local/tmp/server.jar', 'path should be the primary path');
    });

    test('detects a size mismatch that would indicate a corrupt or incomplete push', async () => {
        const EXPECTED_SIZE = 102400;
        const CORRUPT_SIZE  =  51200;  // half the expected size
        const adb = makeAdb(async (args) => {
            const path = args[args.length - 1];
            return `-rw-rw-rw- 1 shell shell ${CORRUPT_SIZE} 2024-01-01 10:00 ${path}`;
        });

        const result = await probeRemoteServer(adb, ['/data/local/tmp/server.jar']);

        assert.equal(result.exists, true);
        assert.notEqual(result.size, EXPECTED_SIZE,
            'probe should return the actual (mismatched) size so the caller can detect the corruption');
    });

    test('reports binary at alt path when primary is absent', async () => {
        const execFn = async (args) => {
            const path = args[args.length - 1];
            if (path === '/data/local/tmp/server.jar') {
                return 'ls: /data/local/tmp/server.jar: No such file or directory';
            }
            return `-rw-rw-rw- 1 shell shell 8192 2024-01-01 10:00 ${path}`;
        };
        const adb = makeAdb(execFn);

        const result = await probeRemoteServer(adb, [
            '/data/local/tmp/server.jar',
            '/sdcard/Download/server.jar',
        ]);
        assert.equal(result.exists, true);
        assert.equal(result.path, '/sdcard/Download/server.jar');
        assert.equal(result.size, 8192);
    });
});
