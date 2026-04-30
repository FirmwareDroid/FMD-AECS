/**
 * Unit tests for source/services/adb/device-backoff.js
 * Run with: node --test source/tests/device-backoff.test.js
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
    getDeviceBackoffRemaining,
    recordDeviceFailure,
    clearDeviceFailure,
    DEFAULT_BACKOFF_BASE_MS,
    DEFAULT_BACKOFF_MAX_MS,
} from '../services/adb/device-backoff.js';

// ─── getDeviceBackoffRemaining ────────────────────────────────────────────────

describe('getDeviceBackoffRemaining', () => {
    test('returns 0 for an unknown serial', () => {
        const map = new Map();
        assert.equal(getDeviceBackoffRemaining(map, 'emulator-5554'), 0);
    });

    test('returns 0 for a null serial', () => {
        const map = new Map();
        assert.equal(getDeviceBackoffRemaining(map, null), 0);
    });

    test('returns 0 for an undefined serial', () => {
        const map = new Map();
        assert.equal(getDeviceBackoffRemaining(map, undefined), 0);
    });

    test('returns 0 when back-off window has elapsed', () => {
        const map = new Map();
        const baseMs = 5_000;
        // count=1 → backoff=5s; lastFailTs=long ago
        map.set('dev-1', { count: 1, lastFailTs: Date.now() - baseMs - 1 });
        assert.equal(getDeviceBackoffRemaining(map, 'dev-1', baseMs), 0);
    });

    test('returns positive remaining ms while inside back-off window', () => {
        const map = new Map();
        const baseMs = 5_000;
        map.set('dev-1', { count: 1, lastFailTs: Date.now() });
        const remaining = getDeviceBackoffRemaining(map, 'dev-1', baseMs);
        assert.ok(remaining > 0 && remaining <= baseMs, `expected 0 < remaining <= ${baseMs}, got ${remaining}`);
    });

    test('back-off doubles with each failure count (exponential)', () => {
        const baseMs = 1_000;
        const maxMs  = 60_000;
        const map = new Map();
        const now = Date.now();

        // count=1 → 1s; count=2 → 2s; count=3 → 4s
        for (const [count, expectedMs] of [[1, 1000], [2, 2000], [3, 4000]]) {
            map.set('dev', { count, lastFailTs: now });
            const remaining = getDeviceBackoffRemaining(map, 'dev', baseMs, maxMs);
            assert.ok(
                remaining > expectedMs - 50 && remaining <= expectedMs,
                `count=${count}: expected ~${expectedMs}ms, got ${remaining}ms`
            );
        }
    });

    test('back-off is capped at maxMs', () => {
        const baseMs = 5_000;
        const maxMs  = 10_000;
        const map = new Map();
        // count=10 → raw=5s*2^9=2560s → capped to 10s
        map.set('dev', { count: 10, lastFailTs: Date.now() });
        const remaining = getDeviceBackoffRemaining(map, 'dev', baseMs, maxMs);
        assert.ok(remaining <= maxMs, `expected remaining <= ${maxMs}, got ${remaining}`);
    });
});

// ─── recordDeviceFailure ──────────────────────────────────────────────────────

describe('recordDeviceFailure', () => {
    test('returns 1 on first failure for a new serial', () => {
        const map = new Map();
        const count = recordDeviceFailure(map, 'dev-1');
        assert.equal(count, 1);
        assert.equal(map.get('dev-1').count, 1);
    });

    test('increments count on subsequent calls', () => {
        const map = new Map();
        recordDeviceFailure(map, 'dev-1');
        recordDeviceFailure(map, 'dev-1');
        const count = recordDeviceFailure(map, 'dev-1');
        assert.equal(count, 3);
        assert.equal(map.get('dev-1').count, 3);
    });

    test('updates lastFailTs to approximately now', () => {
        const map = new Map();
        const before = Date.now();
        recordDeviceFailure(map, 'dev-1');
        const after = Date.now();
        const ts = map.get('dev-1').lastFailTs;
        assert.ok(ts >= before && ts <= after, `expected timestamp between ${before} and ${after}, got ${ts}`);
    });

    test('returns 0 and does not mutate map for a null serial', () => {
        const map = new Map();
        const count = recordDeviceFailure(map, null);
        assert.equal(count, 0);
        assert.equal(map.size, 0);
    });

    test('returns 0 and does not mutate map for an undefined serial', () => {
        const map = new Map();
        const count = recordDeviceFailure(map, undefined);
        assert.equal(count, 0);
        assert.equal(map.size, 0);
    });

    test('tracks multiple serials independently', () => {
        const map = new Map();
        recordDeviceFailure(map, 'dev-A');
        recordDeviceFailure(map, 'dev-A');
        recordDeviceFailure(map, 'dev-B');
        assert.equal(map.get('dev-A').count, 2);
        assert.equal(map.get('dev-B').count, 1);
    });
});

// ─── clearDeviceFailure ───────────────────────────────────────────────────────

describe('clearDeviceFailure', () => {
    test('removes an existing entry', () => {
        const map = new Map();
        recordDeviceFailure(map, 'dev-1');
        clearDeviceFailure(map, 'dev-1');
        assert.ok(!map.has('dev-1'));
    });

    test('is a no-op for an unknown serial', () => {
        const map = new Map();
        assert.doesNotThrow(() => clearDeviceFailure(map, 'unknown'));
    });

    test('is a no-op for a null serial', () => {
        const map = new Map();
        assert.doesNotThrow(() => clearDeviceFailure(map, null));
    });

    test('back-off is 0 after clearing a previously failing serial', () => {
        const map = new Map();
        recordDeviceFailure(map, 'dev-1');
        recordDeviceFailure(map, 'dev-1');
        clearDeviceFailure(map, 'dev-1');
        assert.equal(getDeviceBackoffRemaining(map, 'dev-1'), 0);
    });
});

// ─── Default export constants ─────────────────────────────────────────────────

describe('exported constants', () => {
    test('DEFAULT_BACKOFF_BASE_MS is a positive number', () => {
        assert.ok(typeof DEFAULT_BACKOFF_BASE_MS === 'number' && DEFAULT_BACKOFF_BASE_MS > 0);
    });

    test('DEFAULT_BACKOFF_MAX_MS is greater than DEFAULT_BACKOFF_BASE_MS', () => {
        assert.ok(DEFAULT_BACKOFF_MAX_MS > DEFAULT_BACKOFF_BASE_MS);
    });
});
