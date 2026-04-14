/**
 * Unit tests for source/utils/auth.js
 * Run with: node --test source/tests/auth.test.js
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { validateBasicAuthHeader, isAuthEnabled } from '../utils/auth.js';

// Store original env
let origUser, origPass, origEnabled;
beforeEach(() => {
    origUser = process.env.AUTH_USER;
    origPass = process.env.AUTH_PASS;
    origEnabled = process.env.AUTH_ENABLED;
    process.env.AUTH_USER = 'testuser';
    process.env.AUTH_PASS = 'testpass';
    process.env.AUTH_ENABLED = 'true';
});
afterEach(() => {
    if (origUser === undefined) delete process.env.AUTH_USER; else process.env.AUTH_USER = origUser;
    if (origPass === undefined) delete process.env.AUTH_PASS; else process.env.AUTH_PASS = origPass;
    if (origEnabled === undefined) delete process.env.AUTH_ENABLED; else process.env.AUTH_ENABLED = origEnabled;
});

test('validateBasicAuthHeader: returns false for null', () => {
    assert.equal(validateBasicAuthHeader(null), false);
});

test('validateBasicAuthHeader: returns false for empty string', () => {
    assert.equal(validateBasicAuthHeader(''), false);
});

test('validateBasicAuthHeader: accepts correct credentials', () => {
    const encoded = Buffer.from('testuser:testpass').toString('base64');
    assert.equal(validateBasicAuthHeader(`Basic ${encoded}`), true);
});

test('validateBasicAuthHeader: rejects wrong password', () => {
    const encoded = Buffer.from('testuser:wrongpass').toString('base64');
    assert.equal(validateBasicAuthHeader(`Basic ${encoded}`), false);
});

test('validateBasicAuthHeader: rejects wrong username', () => {
    const encoded = Buffer.from('wronguser:testpass').toString('base64');
    assert.equal(validateBasicAuthHeader(`Basic ${encoded}`), false);
});

test('validateBasicAuthHeader: rejects malformed header (no scheme)', () => {
    assert.equal(validateBasicAuthHeader('not-base64-or-valid'), false);
});

test('validateBasicAuthHeader: accepts bare base64 payload', () => {
    const encoded = Buffer.from('testuser:testpass').toString('base64');
    assert.equal(validateBasicAuthHeader(encoded), true);
});

test('validateBasicAuthHeader: rejects wrong scheme', () => {
    const encoded = Buffer.from('testuser:testpass').toString('base64');
    assert.equal(validateBasicAuthHeader(`Bearer ${encoded}`), false);
});

test('isAuthEnabled: returns true when AUTH_ENABLED=true', () => {
    process.env.AUTH_ENABLED = 'true';
    assert.equal(isAuthEnabled(), true);
});

test('isAuthEnabled: returns true when AUTH_ENABLED=1', () => {
    process.env.AUTH_ENABLED = '1';
    assert.equal(isAuthEnabled(), true);
});

test('isAuthEnabled: returns false when AUTH_ENABLED=false', () => {
    process.env.AUTH_ENABLED = 'false';
    assert.equal(isAuthEnabled(), false);
});

test('isAuthEnabled: returns false when AUTH_ENABLED is unset', () => {
    delete process.env.AUTH_ENABLED;
    assert.equal(isAuthEnabled(), false);
});
