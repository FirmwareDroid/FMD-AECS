/**
 * Unit tests for source/services/adb/adb-device-monitor.js
 * Run with: node --test source/tests/device-monitor.test.js
 */
import { test, describe, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { createDeviceMonitor } from '../services/adb/adb-device-monitor.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeLogger() {
    const calls = { info: [], debug: [], error: [] };
    return {
        info:  (...a) => calls.info.push(a),
        debug: (...a) => calls.debug.push(a),
        error: (...a) => calls.error.push(a),
        calls,
    };
}

function makePool(key, devices = []) {
    return {
        key,
        client: {
            getDevices: async () => devices,
            createTransport: async ({ serial }) => ({ serial }),
        },
    };
}

// ---------------------------------------------------------------------------
// pushToAllDevices
// ---------------------------------------------------------------------------

describe('createDeviceMonitor — pushToAllDevices', () => {
    test('does nothing when getPools returns an empty array', async () => {
        const logger = makeLogger();
        const pushed = [];
        const { pushToAllDevices } = createDeviceMonitor({
            logger,
            getPools: () => [],
            Adb: class { constructor(t) { this.transport = t; } },
            pushServer: async (adb) => { pushed.push(adb); },
        });

        await pushToAllDevices();

        assert.equal(pushed.length, 0, 'expected no pushes when there are no pools');
    });

    test('does nothing when pool returns no devices', async () => {
        const logger = makeLogger();
        const pushed = [];
        const { pushToAllDevices } = createDeviceMonitor({
            logger,
            getPools: () => [makePool('localhost:5037', [])],
            Adb: class { constructor(t) { this.transport = t; } },
            pushServer: async (adb) => { pushed.push(adb); },
        });

        await pushToAllDevices();

        assert.equal(pushed.length, 0, 'expected no pushes when pool has no devices');
    });

    test('pushes to a single device in a single pool', async () => {
        const logger = makeLogger();
        const pushed = [];
        const { pushToAllDevices } = createDeviceMonitor({
            logger,
            getPools: () => [makePool('localhost:5037', [{ serial: 'emulator-5554' }])],
            Adb: class { constructor(t) { this.transport = t; } },
            pushServer: async (adb) => { pushed.push(adb); },
        });

        await pushToAllDevices();

        assert.equal(pushed.length, 1, 'expected exactly one push');
    });

    test('pushes once per pool (first device only)', async () => {
        const logger = makeLogger();
        const pushed = [];
        const pool1 = makePool('host1:5037', [{ serial: 'device-A' }]);
        const pool2 = makePool('host2:5037', [{ serial: 'device-B' }]);
        const { pushToAllDevices } = createDeviceMonitor({
            logger,
            getPools: () => [pool1, pool2],
            Adb: class { constructor(t) { this.transport = t; } },
            pushServer: async (adb) => { pushed.push(adb); },
        });

        await pushToAllDevices();

        assert.equal(pushed.length, 2, 'expected one push per pool');
    });

    test('passes the Adb instance created from the transport to pushServer', async () => {
        const logger = makeLogger();
        const receivedTransports = [];

        class MockAdb {
            constructor(transport) { this.transport = transport; }
        }

        const pool = makePool('localhost:5037', [{ serial: 'device-1' }]);
        const { pushToAllDevices } = createDeviceMonitor({
            logger,
            getPools: () => [pool],
            Adb: MockAdb,
            pushServer: async (adb) => { receivedTransports.push(adb.transport); },
        });

        await pushToAllDevices();

        assert.equal(receivedTransports.length, 1);
        assert.deepEqual(receivedTransports[0], { serial: 'device-1' });
    });

    test('logs an error and continues when getDevices fails for a pool', async () => {
        const logger = makeLogger();
        const pushed = [];
        const faultyPool = {
            key: 'bad:5037',
            client: {
                getDevices: async () => { throw new Error('connection refused'); },
                createTransport: async ({ serial }) => ({ serial }),
            },
        };
        const goodPool = makePool('good:5037', [{ serial: 'ok-device' }]);

        const { pushToAllDevices } = createDeviceMonitor({
            logger,
            getPools: () => [faultyPool, goodPool],
            Adb: class { constructor(t) { this.transport = t; } },
            pushServer: async (adb) => { pushed.push(adb); },
        });

        await pushToAllDevices();

        assert.equal(pushed.length, 1, 'should still push to the healthy pool device');
        assert.ok(
            logger.calls.error.some((args) => args.join(' ').includes('bad:5037')),
            'expected error log for the faulty pool'
        );
    });

    test('logs an error and continues when pushServer fails for a device', async () => {
        const logger = makeLogger();
        const pushed = [];
        const failPool = makePool('fail:5037', [{ serial: 'fail-device' }]);
        const okPool   = makePool('ok:5037',   [{ serial: 'ok-device' }]);

        const { pushToAllDevices } = createDeviceMonitor({
            logger,
            getPools: () => [failPool, okPool],
            Adb: class { constructor(t) { this.transport = t; } },
            pushServer: async (adb) => {
                if (adb.transport.serial === 'fail-device') throw new Error('push failed');
                pushed.push(adb);
            },
        });

        await pushToAllDevices();

        assert.equal(pushed.length, 1, 'should still push to the device that did not fail');
        assert.ok(
            logger.calls.error.some((args) => args.join(' ').includes('fail-device')),
            'expected error log for the failed push'
        );
    });

    test('skips device entries that have no serial', async () => {
        const logger = makeLogger();
        const pushed = [];
        const pool = {
            key: 'localhost:5037',
            client: {
                getDevices: async () => [{ serial: '' }],
                createTransport: async ({ serial }) => ({ serial }),
            },
        };

        const { pushToAllDevices } = createDeviceMonitor({
            logger,
            getPools: () => [pool],
            Adb: class { constructor(t) { this.transport = t; } },
            pushServer: async (adb) => { pushed.push(adb); },
        });

        await pushToAllDevices();

        assert.equal(pushed.length, 0, 'expected no push for a device without a serial');
    });

    test('getPools is called freshly on each invocation (reflects pool refreshes)', async () => {
        const logger = makeLogger();
        const pushed = [];
        let poolList = [];

        const { pushToAllDevices } = createDeviceMonitor({
            logger,
            getPools: () => poolList,
            Adb: class { constructor(t) { this.transport = t; } },
            pushServer: async (adb) => { pushed.push(adb); },
        });

        // First call: no pools
        await pushToAllDevices();
        assert.equal(pushed.length, 0);

        // Simulate a pool being added after startup
        poolList = [makePool('localhost:5037', [{ serial: 'new-device' }])];

        // Second call: pool now available
        await pushToAllDevices();
        assert.equal(pushed.length, 1, 'should push after pool becomes available');
    });
});

// ---------------------------------------------------------------------------
// start / stop
// ---------------------------------------------------------------------------

describe('createDeviceMonitor — start / stop', () => {
    test('start returns a timer handle and stop clears it without throwing', () => {
        const logger = makeLogger();
        const { start, stop } = createDeviceMonitor({
            logger,
            getPools: () => [],
            Adb: class {},
            pushServer: async () => {},
        });

        const timer = start(60_000);
        assert.ok(timer, 'expected a timer handle');
        assert.doesNotThrow(() => stop(timer));
    });

    test('start triggers an immediate push call', async () => {
        const logger = makeLogger();
        const callCount = { pushToAllDevices: 0 };

        const { start, stop } = createDeviceMonitor({
            logger,
            getPools: () => [makePool('localhost:5037', [{ serial: 'device-1' }])],
            Adb: class { constructor(t) { this.transport = t; } },
            pushServer: async () => { callCount.pushToAllDevices++; },
        });

        const timer = start(60_000);

        // Allow the immediate microtask / promise to resolve
        await new Promise((resolve) => setTimeout(resolve, 50));
        stop(timer);

        assert.ok(callCount.pushToAllDevices >= 1, 'expected at least one push on start');
    });

    test('stop(undefined) does not throw', () => {
        const { stop } = createDeviceMonitor({
            logger: makeLogger(),
            getPools: () => [],
            Adb: class {},
            pushServer: async () => {},
        });
        assert.doesNotThrow(() => stop(undefined));
    });
});
