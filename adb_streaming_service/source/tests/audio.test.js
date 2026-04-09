/**
 * Unit tests for source/utils/normalizers/audio.js
 * Run with: node --test source/tests/audio.test.js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateAudioPacket, extractAudioBuffer } from '../utils/normalizers/audio.js';

// ── validateAudioPacket ──────────────────────────────────────────────────────

test('validateAudioPacket: rejects null buffer', () => {
    const r = validateAudioPacket(null, { codec: 'raw', channels: 2 });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'no-binary-buffer');
});

test('validateAudioPacket: rejects empty buffer', () => {
    const r = validateAudioPacket(new Uint8Array(0), { codec: 'raw', channels: 2 });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'empty-buffer');
});

test('validateAudioPacket: raw PCM aligned buffer passes', () => {
    // stereo Int16LE: 4 bytes per frame. 40 bytes = 10 frames. Non-zero.
    const buf = Buffer.alloc(40, 0x42);
    const r = validateAudioPacket(buf, { codec: 'raw', channels: 2 });
    assert.equal(r.ok, true);
});

test('validateAudioPacket: raw PCM misaligned buffer fails', () => {
    // stereo: 4 bytes per frame. 41 bytes is not divisible.
    const buf = Buffer.alloc(41, 0x42);
    const r = validateAudioPacket(buf, { codec: 'raw', channels: 2 });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'not-multiple-of-frame');
});

test('validateAudioPacket: all-zero raw PCM fails', () => {
    const buf = Buffer.alloc(40, 0x00);
    const r = validateAudioPacket(buf, { codec: 'raw', channels: 2 });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'all-zero-preview');
});

test('validateAudioPacket: AAC ADTS syncword passes', () => {
    // ADTS syncword: 0xFF 0xF1 ...
    const buf = Buffer.from([0xFF, 0xF1, 0x50, 0x80, 0x00, 0x1F, 0xFC]);
    const r = validateAudioPacket(buf, { codec: 'aac' });
    assert.equal(r.ok, true);
    assert.equal(r.details.adts, true);
});

test('validateAudioPacket: AAC without syncword fails', () => {
    const buf = Buffer.from([0x00, 0x11, 0x22]);
    const r = validateAudioPacket(buf, { codec: 'aac' });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'aac-no-adts-syncword');
});

test('validateAudioPacket: Opus OGG header passes', () => {
    // Ogg Opus magic: 'Opus' = 0x4F 0x70 0x75 0x73
    const buf = Buffer.from([0x4F, 0x70, 0x75, 0x73, 0x48, 0x65, 0x61, 0x64]);
    const r = validateAudioPacket(buf, { codec: 'opus' });
    assert.equal(r.ok, true);
});

test('validateAudioPacket: unknown codec accepted with note', () => {
    const buf = Buffer.from([0x01, 0x02, 0x03, 0x04]);
    const r = validateAudioPacket(buf, { codec: 'flac' });
    assert.equal(r.ok, true);
    assert.equal(r.details.note, 'unknown-codec-accepted');
});

// ── extractAudioBuffer ───────────────────────────────────────────────────────

test('extractAudioBuffer: returns null buf for null packet', () => {
    const r = extractAudioBuffer(null);
    assert.equal(r.buf, null);
});

test('extractAudioBuffer: handles Uint8Array directly', () => {
    const arr = new Uint8Array([1, 2, 3]);
    const r = extractAudioBuffer(arr);
    assert.deepEqual(r.buf, arr);
    assert.equal(r.pktLen, 3);
});

test('extractAudioBuffer: handles Buffer', () => {
    const buf = Buffer.from([1, 2, 3]);
    const r = extractAudioBuffer(buf);
    assert.ok(r.buf);
    assert.equal(r.pktLen, 3);
});

test('extractAudioBuffer: handles object with data property', () => {
    const inner = Buffer.from([10, 20, 30]);
    const r = extractAudioBuffer({ data: inner });
    assert.ok(r.buf);
    assert.equal(r.pktLen, 3);
});
