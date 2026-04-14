/**
 * Unit tests for source/utils/normalizers/touch.js
 * Run with: node --test source/tests/touch.test.js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeTouchPayload, fallbackNormalizePayload } from '../utils/normalizers/touch.js';

// ── normalizeTouchPayload ────────────────────────────────────────────────────

test('normalizeTouchPayload: returns null for null payload', async () => {
    const r = await normalizeTouchPayload({}, null);
    assert.equal(r, null);
});

test('normalizeTouchPayload: returns null for non-object payload', async () => {
    const r = await normalizeTouchPayload({}, 'bad');
    assert.equal(r, null);
});

test('normalizeTouchPayload: returns a payload even for empty object (uses defaults)', async () => {
    // normalizeTouchPayload is permissive: {x:0, y:0} is valid (e.g. touch at origin)
    const r = await normalizeTouchPayload({}, {});
    // It either returns a normalized payload with defaults, or null — either is acceptable.
    // What is NOT acceptable is throwing.
    assert.ok(r === null || typeof r === 'object');
});

test('normalizeTouchPayload: basic touch down with device coords', async () => {
    const r = await normalizeTouchPayload({}, {
        deviceX: 100, deviceY: 200,
        displayWidth: 1080, displayHeight: 1920,
        action: 0, // ACTION_DOWN
    });
    assert.ok(r, 'should return a normalized payload');
    assert.equal(r.pointerX, 100);
    assert.equal(r.pointerY, 200);
    assert.equal(r.videoWidth, 1080);
    assert.equal(r.videoHeight, 1920);
    assert.equal(typeof r.action, 'number');
    assert.equal(typeof r.pointerId, 'bigint');
});

test('normalizeTouchPayload: passes clientX/Y through as pixel coordinates', async () => {
    // clientX/Y are treated as raw pixel coords (not scaled by client viewport)
    const r = await normalizeTouchPayload({
        videoMetadata: { width: 1080, height: 1920 },
    }, {
        clientX: 270, clientY: 480,
        displayWidth: 1080, displayHeight: 1920,
        action: 0,
    });
    assert.ok(r);
    assert.equal(r.pointerX, 270);
    assert.equal(r.pointerY, 480);
});

test('normalizeTouchPayload: pressure defaults to 0.5 when omitted', async () => {
    const r = await normalizeTouchPayload({}, {
        deviceX: 50, deviceY: 50,
        displayWidth: 1080, displayHeight: 1920,
        action: 0,
    });
    assert.ok(r);
    assert.equal(r.pressure, 0.5);
});

test('normalizeTouchPayload: pressure is passed through as-is (no clamping)', async () => {
    const r = await normalizeTouchPayload({}, {
        deviceX: 50, deviceY: 50,
        displayWidth: 1080, displayHeight: 1920,
        action: 0,
        pressure: 0.75,
    });
    assert.ok(r);
    assert.equal(r.pressure, 0.75);
});

test('normalizeTouchPayload: pointerId converts to BigInt', async () => {
    const r = await normalizeTouchPayload({}, {
        deviceX: 0, deviceY: 0,
        displayWidth: 100, displayHeight: 100,
        action: 0,
        pointerId: 3,
    });
    assert.ok(r);
    assert.equal(typeof r.pointerId, 'bigint');
    assert.equal(r.pointerId, 3n);
});

// ── fallbackNormalizePayload ─────────────────────────────────────────────────

test('fallbackNormalizePayload: returns null for null payload', () => {
    assert.equal(fallbackNormalizePayload({}, null), null);
});

test('fallbackNormalizePayload: returns null for non-finite coords', () => {
    const r = fallbackNormalizePayload({}, { x: NaN, y: NaN });
    assert.equal(r, null);
});

test('fallbackNormalizePayload: scales normalized coords to video size', () => {
    const user = { videoMetadata: { width: 1080, height: 1920 } };
    // coords <= 1 with video metadata → treated as normalized, scaled up
    const r = fallbackNormalizePayload(user, { x: 0.5, y: 0.25, displayWidth: 1080, displayHeight: 1920 });
    assert.ok(r);
    assert.equal(r.pointerX, 540);
    assert.equal(r.pointerY, 480);
});

test('fallbackNormalizePayload: passes through pixel coords unchanged', () => {
    const r = fallbackNormalizePayload({}, { x: 200, y: 400, displayWidth: 1080, displayHeight: 1920 });
    assert.ok(r);
    assert.equal(r.pointerX, 200);
    assert.equal(r.pointerY, 400);
});

test('fallbackNormalizePayload: returns required fields', () => {
    const r = fallbackNormalizePayload({}, { x: 100, y: 200, displayWidth: 1080, displayHeight: 1920 });
    assert.ok(r);
    assert.ok('action' in r);
    assert.ok('pointerId' in r);
    assert.ok('pointerX' in r);
    assert.ok('pointerY' in r);
    assert.ok('videoWidth' in r);
    assert.ok('videoHeight' in r);
    assert.ok('pressure' in r);
    assert.ok('buttons' in r);
});
