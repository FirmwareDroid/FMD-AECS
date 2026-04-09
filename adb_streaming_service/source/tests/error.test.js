/**
 * Unit tests for source/utils/error.js
 * Run with: node --test source/tests/error.test.js
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { serializeError } from '../utils/error.js';

test('serializeError: returns null for falsy input', () => {
    assert.equal(serializeError(null), null);
    assert.equal(serializeError(undefined), null);
    assert.equal(serializeError(0), null);
    assert.equal(serializeError(''), null);
});

test('serializeError: serializes a standard Error', () => {
    const err = new Error('test error');
    const result = serializeError(err);
    assert.equal(result.name, 'Error');
    assert.equal(result.message, 'test error');
    assert.ok(typeof result.stack === 'string');
});

test('serializeError: includes custom properties', () => {
    const err = new Error('with code');
    err.code = 'ENOENT';
    err.errno = -2;
    const result = serializeError(err);
    assert.equal(result.code, 'ENOENT');
    assert.equal(result.errno, -2);
});

test('serializeError: serializes error with cause', () => {
    const cause = new Error('root cause');
    const err = new Error('top level', { cause });
    const result = serializeError(err);
    assert.ok(result.cause);
    assert.equal(result.cause.message, 'root cause');
});

test('serializeError: handles non-Error thrown values', () => {
    const result = serializeError('string thrown');
    assert.ok(result);
    assert.ok(typeof result.message === 'string');
});

test('serializeError: handles plain objects', () => {
    const result = serializeError({ message: 'plain object' });
    assert.ok(result);
});
