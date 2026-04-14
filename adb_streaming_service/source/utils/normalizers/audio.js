import { logger } from '../../services/logger.js';

/**
 * Generic fallback: extract a raw binary buffer from an audio packet of unknown shape.
 *
 * @param {*} packet
 * @returns {{ buf: Uint8Array|null, pktType: string, pktLen: number|null, info: object|null }}
 */
export function extractAudioBuffer(packet) {
    let buf = null;
    let pktType = typeof packet;
    let pktLen = null;
    let info = null;

    if (!packet) return { buf, pktType, pktLen, info };

    if (packet instanceof Uint8Array || (typeof Buffer !== 'undefined' && Buffer.isBuffer(packet))) {
        buf = packet;
        pktType = 'Uint8Array';
        pktLen = packet.length;
        return { buf, pktType, pktLen, info };
    }

    if (typeof ArrayBuffer !== 'undefined' && ArrayBuffer.isView && ArrayBuffer.isView(packet)) {
        buf = new Uint8Array(packet.buffer, packet.byteOffset || 0, packet.byteLength);
        pktType = 'ArrayBufferView';
        pktLen = buf.length;
        return { buf, pktType, pktLen, info };
    }

    if (typeof ArrayBuffer !== 'undefined' && packet instanceof ArrayBuffer) {
        buf = new Uint8Array(packet);
        pktType = 'ArrayBuffer';
        pktLen = buf.length;
        return { buf, pktType, pktLen, info };
    }

    if (packet && typeof packet === 'object') {
        const keys = Object.keys(packet);
        info = { keys };
        // try common buffer property names
        for (const key of ['data', 'buffer', 'bytes', 'payload', 'content', 'body']) {
            const val = packet[key];
            if (!val) continue;
            if (val instanceof Uint8Array || (typeof Buffer !== 'undefined' && Buffer.isBuffer(val))) {
                buf = val;
                pktType = `Wrapped(${key}:Uint8Array)`;
                pktLen = val.length;
                break;
            }
            if (typeof ArrayBuffer !== 'undefined' && ArrayBuffer.isView && ArrayBuffer.isView(val)) {
                buf = new Uint8Array(val.buffer, val.byteOffset || 0, val.byteLength);
                pktType = `Wrapped(${key}:ArrayBufferView)`;
                pktLen = buf.length;
                break;
            }
            if (typeof ArrayBuffer !== 'undefined' && val instanceof ArrayBuffer) {
                buf = new Uint8Array(val);
                pktType = `Wrapped(${key}:ArrayBuffer)`;
                pktLen = buf.length;
                break;
            }
        }
    }

    return { buf, pktType, pktLen, info };
}

/**
 * Validate an incoming audio packet for common codecs and raw PCM expectations.
 *
 * @param {Uint8Array|Buffer|null} buf
 * @param {object} metadata
 * @param {string} sessionId
 * @param {object} [wsObj]
 * @returns {{ ok: boolean, reason?: string, details: object }}
 */
export function validateAudioPacket(buf, metadata = {}, sessionId = '<unknown>', wsObj = undefined) {
    try {
        const details = {
            sessionId,
            codec: metadata?.codec || metadata?.audioCodec || (wsObj && wsObj.audioCodec) || 'raw',
            sampleRate: metadata?.sampleRate || metadata?.sample_rate || metadata?.rate || 48000,
            channels: metadata?.channels || metadata?.channelCount || 2,
        };
        if (!buf) return { ok: false, reason: 'no-binary-buffer', details };
        const len = typeof buf.length === 'number' ? buf.length : null;
        details.length = len;
        if (!len || len === 0) return { ok: false, reason: 'empty-buffer', details };

        const codec = String(details.codec || 'raw').toLowerCase();

        if (codec === 'raw' || codec === 'pcm' || codec === 'pcm16' || codec === 'rawpcm') {
            const frameBytes = (Number(details.channels) || 1) * 2;
            if (frameBytes <= 0) return { ok: false, reason: 'invalid-channels', details };
            details.frameBytes = frameBytes;
            if (len % frameBytes !== 0) {
                details.remainder = len % frameBytes;
                return { ok: false, reason: 'not-multiple-of-frame', details };
            }
            // sanity: check not all zeros
            let nonZero = false;
            const view = buf instanceof Uint8Array ? buf : Buffer.from(buf);
            for (let i = 0; i < Math.min(64, view.length); i++) {
                if (view[i] !== 0) { nonZero = true; break; }
            }
            if (!nonZero) {
                details.preview = Array.from(view.slice(0, 16));
                return { ok: false, reason: 'all-zero-preview', details };
            }
            return { ok: true, details };
        }

        // AAC ADTS check: syncword 0xFFF at start (12 bits set)
        if (codec.includes('aac')) {
            const view = buf instanceof Uint8Array ? buf : Buffer.from(buf);
            if (view.length >= 2) {
                const sync = ((view[0] & 0xFF) << 4) | ((view[1] & 0xF0) >> 4);
                if ((sync & 0xFFF) === 0xFFF) return { ok: true, details: Object.assign(details, { adts: true }) };
                return { ok: false, reason: 'aac-no-adts-syncword', details };
            }
            return { ok: false, reason: 'aac-too-short', details };
        }

        // Opus heuristic
        if (codec.includes('opus')) {
            const view = buf instanceof Uint8Array ? buf : Buffer.from(buf);
            if (view.length >= 4) {
                if (view[0] === 0x4f && view[1] === 0x70 && view[2] === 0x75 && view[3] === 0x73) {
                    return { ok: true, details: Object.assign(details, { ogg: true }) };
                }
                if (view.length < 10) return { ok: false, reason: 'opus-too-short', details };
                return { ok: true, details };
            }
            return { ok: false, reason: 'opus-too-short', details };
        }

        // Unknown codec: accept but warn
        return { ok: true, details: Object.assign(details, { note: 'unknown-codec-accepted' }) };
    } catch (e) {
        return { ok: false, reason: 'validation-exception', error: String(e), details: { sessionId } };
    }
}
