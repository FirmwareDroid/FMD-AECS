/**
 * Unit tests for source/utils/normalizers/scroll.js
 * Run with: node --test source/tests/scroll.test.js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeScrollPayload } from '../utils/normalizers/scroll.js';

test('normalizeScrollPayload: returns null for null payload', () => {
    assert.equal(normalizeScrollPayload({}, null), null);
});

test('normalizeScrollPayload: returns null for non-object payload', () => {
    assert.equal(normalizeScrollPayload({}, 'bad'), null);
});

test('normalizeScrollPayload: basic scroll down produces negative scrollY', () => {
    // pixel delta deltaY=100 over a 1000px-tall screen → normalized = -(100/1000) = -0.1
    const result = normalizeScrollPayload({}, {
        x: 500, y: 500,
        deltaY: 100, deltaX: 0,
        displayWidth: 1000, displayHeight: 1000,
    });
    assert.ok(result);
    assert.ok(result.scrollY < 0, `expected scrollY < 0, got ${result.scrollY}`);
});

test('normalizeScrollPayload: clamps to [-1, 1]', () => {
    // extremely large delta → should clamp
    const result = normalizeScrollPayload({}, {
        x: 0, y: 0,
        deltaY: 999999, deltaX: 999999,
        displayWidth: 100, displayHeight: 100,
    });
    assert.ok(result);
    assert.ok(result.scrollY >= -1 && result.scrollY <= 1);
    assert.ok(result.scrollX >= -1 && result.scrollX <= 1);
});

test('normalizeScrollPayload: zero deltas produce zero scroll', () => {
    const result = normalizeScrollPayload({}, {
        x: 100, y: 100,
        deltaY: 0, deltaX: 0,
        displayWidth: 800, displayHeight: 600,
    });
    assert.ok(result);
    assert.equal(result.scrollX, 0);
    assert.equal(result.scrollY, 0);
});

test('normalizeScrollPayload: pointer coordinates are preserved', () => {
    const result = normalizeScrollPayload({}, {
        x: 250, y: 375,
        deltaY: 0, deltaX: 0,
        displayWidth: 500, displayHeight: 750,
    });
    assert.ok(result);
    assert.equal(result.x, 250);
    assert.equal(result.y, 375);
});

test('normalizeScrollPayload: uses videoMetadata from user when no display size in payload', () => {
    const user = { videoMetadata: { width: 1080, height: 1920 } };
    const result = normalizeScrollPayload(user, {
        x: 100, y: 100,
        deltaY: 192, deltaX: 0,
    });
    assert.ok(result);
    // 192/1920 = 0.1, inverted = -0.1
    assert.ok(Math.abs(result.scrollY - (-0.1)) < 0.001, `expected ~-0.1, got ${result.scrollY}`);
});
