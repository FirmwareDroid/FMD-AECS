/**
 * WebSocket `message` handler.
 *
 * Routes incoming msgpack-encoded commands to the appropriate scrcpy controller
 * method (injectTouch, injectScroll, injectKeyCode, setVolume, …).
 *
 * @param {object} deps - Injected dependencies.
 * @param {object} deps.unpacker
 * @param {object} deps.packer
 * @param {object} deps.logger
 * @param {object} deps.users              - global.users Map
 * @param {Function} deps.sendToUser
 * @param {Function} deps.serializeError
 * @param {Function} deps.normalizeTouchPayload
 * @param {Function} deps.fallbackNormalizePayload
 * @param {Function} deps.normalizeScrollPayload
 *
 * @returns {Function} uWebSockets.js message callback.
 */
export function createMessageHandler(deps) {
    const {
        unpacker, packer, logger, users, sendToUser,
        serializeError, normalizeTouchPayload, fallbackNormalizePayload, normalizeScrollPayload,
    } = deps;

    // Mapping of common key names to Android KeyEvent codes
    const KEY_NAME_MAP = {
        HOME: 3, BACK: 4, CALL: 5, ENDCALL: 6, POWER: 26,
        VOLUME_UP: 24, VOLUME_DOWN: 25, MUTE: 164, MENU: 82,
        APP_SWITCH: 187, ENTER: 66,
        DPAD_UP: 19, DPAD_DOWN: 20, DPAD_LEFT: 21, DPAD_RIGHT: 22, DPAD_CENTER: 23,
        SPACE: 62, TAB: 61, ESCAPE: 111,
    };

    return function message(ws, message) {
        const { id } = ws;
        try {
            logger.debug(`WS message received from id='${id}'`);
            const user = users.get(id);
            const record = unpacker.unpack(message);

            // Log command description at debug level
            if (logger && typeof logger.debug === 'function') {
                let payloadDesc;
                try {
                    const p = record?.payload;
                    if (p == null) payloadDesc = 'null';
                    else if (p instanceof Uint8Array || (typeof Buffer !== 'undefined' && p instanceof Buffer)) payloadDesc = `binary(len=${p.length})`;
                    else if (typeof p === 'string') payloadDesc = `string(len=${p.length})`;
                    else if (typeof p === 'object') payloadDesc = `object(keys=${Object.keys(p || {}).length})`;
                    else payloadDesc = String(typeof p);
                } catch (e) { payloadDesc = 'unknown'; }
                logger.debug(`WS message received id=${id} cmd=${record?.cmd ?? '<none>'} payload=${payloadDesc}`);
            }

            // ------------------------------------------------------------------
            // Helpers (scoped per message invocation for closure over `user`)
            // ------------------------------------------------------------------

            const safeStringify = _makeSafeStringify();

            const safeInvokeController = async (methodName, payload) => {
                try {
                    if (!user) {
                        logger.error(`safeInvokeController: no user for id=${id} when calling ${methodName}`);
                        return;
                    }
                    const controller = user.client?.controller;
                    if (!controller) {
                        logger.error(`safeInvokeController: no controller for id=${id} method=${methodName}`);
                        return;
                    }
                    if (typeof controller[methodName] !== 'function') {
                        logger.error(`safeInvokeController: method not found: ${methodName} for id=${id}`);
                        logger.debug(`Available methods: ${Object.keys(controller).filter((k) => typeof controller[k] === 'function').join(', ')}`);
                        return;
                    }
                    logger.debug(`Invoking controller.${methodName} id=${id} payload=${safeStringify(payload)}`);

                    let callPayload = payload;

                    if (methodName === 'injectTouch') {
                        try {
                            logger.debug(`normalizeTouchPayload call for id=${id} payload=${safeStringify(payload)}`);
                            let normalized = await normalizeTouchPayload(user, payload);
                            if (!normalized) {
                                logger.debug('normalizeTouchPayload returned null, trying fallback');
                                const fb = fallbackNormalizePayload(user, payload);
                                if (fb) {
                                    normalized = fb;
                                    logger.debug('Fallback normalizer produced a payload');
                                } else {
                                    logger.error(`injectTouch: could not normalize payload. payload=${safeStringify(payload)}`);
                                    try {
                                        ws.send(packer.pack({ media: 'error', message: 'injectTouch: invalid payload' }), true);
                                    } catch (e) { /* ignore */ }
                                    return;
                                }
                            }
                            callPayload = normalized;
                            logger.debug(`injectTouch normalized payload=${safeStringify(callPayload)}`);
                        } catch (e) {
                            const errObj = serializeError(e);
                            logger.error(`injectTouch normalization error: ${safeStringify(errObj)}`);
                            return;
                        }
                    }

                    // Sanitize payload types: BigInt fields, numeric string keys
                    try {
                        if (callPayload && typeof callPayload === 'object') {
                            _sanitizePointerIdFields(callPayload);
                            _convertNonPointerBigInt(callPayload);
                            const numericKeys = ['pointerX', 'pointerY', 'videoWidth', 'videoHeight', 'pressure', 'buttons', 'action'];
                            for (const k of numericKeys) {
                                if (k in callPayload) {
                                    const val = callPayload[k];
                                    if (typeof val === 'string') {
                                        const num = Number(val.trim());
                                        if (!Number.isNaN(num)) callPayload[k] = num;
                                    }
                                    if (typeof callPayload[k] === 'bigint') callPayload[k] = Number(callPayload[k]);
                                }
                            }
                            try {
                                const pid = callPayload.pointerId ?? callPayload.pointer_id ?? callPayload.pid;
                                logger.debug('Sanitized pointerId', { value: pid, type: typeof pid });
                            } catch (e) { /* ignore */ }
                            logger.debug('Calling controller with payload types', Object.keys(callPayload).reduce((acc, k) => { acc[k] = typeof callPayload[k]; return acc; }, {}));
                        }
                    } catch (e) {
                        logger.debug('Error sanitizing callPayload:', e);
                    }

                    const res = controller[methodName](callPayload);
                    if (res && typeof res.then === 'function') await res;
                } catch (err) {
                    logger.error(`Error invoking controller.${methodName} for id=${id}: ${err?.message || err}`);
                    logger.debug(err?.stack || err);
                    try {
                        ws.send(packer.pack({
                            media: 'error',
                            message: `controller.${methodName} failed: ${err?.message || err}`,
                        }), true);
                    } catch (e) { /* ignore */ }
                }
            };

            const pressKey = async (keyCode) => {
                try {
                    await safeInvokeController('injectKeyCode', { action: 0, keyCode });
                    await new Promise((r) => setTimeout(r, 50));
                    await safeInvokeController('injectKeyCode', { action: 1, keyCode });
                } catch (e) {
                    logger.error(`pressKey failed for keyCode=${keyCode}: ${e?.message || e}`);
                }
            };

            // ------------------------------------------------------------------
            // Special: audio_ready signal
            // ------------------------------------------------------------------
            if (record?.cmd === 'audio_ready') {
                if (user) user.audioReady = true;
                return;
            }

            // ------------------------------------------------------------------
            // setVolume — handled even when controller is absent (needs controller for key injection)
            // ------------------------------------------------------------------
            const CMD_ALIASES = {
                injectPointer: 'injectTouch',
                injectMouse: 'injectTouch',
                pointer: 'injectTouch',
                setDeviceVolume: 'setVolume',
                setvolume: 'setVolume',
                setVolume: 'setVolume',
            };
            const incomingCmd = record?.cmd;
            const normalizedCmd = CMD_ALIASES[incomingCmd] || incomingCmd;

            if (normalizedCmd === 'setVolume') {
                (async () => {
                    try {
                        logger.debug(`setVolume requested for id=${id} payload=${safeStringify(record.payload)}`);
                        const payload = record.payload || {};

                        if (!user?.client?.controller) {
                            logger.error('setVolume: controller not available');
                            try {
                                sendToUser(user, packer.pack({ media: 'message', type: 'volume_set', payload: { ok: false, error: 'controller-not-available' } }), true);
                            } catch (e) { /* ignore */ }
                            return;
                        }

                        let action = null;
                        let targetPercent = null;
                        let deltaSteps = null;

                        if (typeof payload === 'string') action = payload.toLowerCase();
                        else if (typeof payload === 'number') {
                            const v = payload;
                            if (v >= 0 && v <= 1) targetPercent = Math.round(v * 100);
                            else if (v > 1 && v <= 100) targetPercent = Math.round(v);
                            else targetPercent = Math.round(Math.max(0, Math.min(100, v)));
                        } else if (payload && typeof payload === 'object') {
                            if (typeof payload.delta === 'number') deltaSteps = Math.trunc(payload.delta);
                            if (typeof payload.volume === 'number') {
                                const v = payload.volume;
                                if (v >= 0 && v <= 1) targetPercent = Math.round(v * 100);
                                else if (v > 1 && v <= 100) targetPercent = Math.round(v);
                            }
                            if (typeof payload.action === 'string') action = payload.action.toLowerCase();
                            if (typeof payload.muted === 'boolean') {
                                action = payload.muted ? 'mute' : 'up';
                            }
                        }

                        // Small-step edge-cases for exact 0 or 1 values
                        try {
                            const volVal = (typeof payload === 'number') ? payload
                                : (payload && typeof payload === 'object' && typeof payload.volume === 'number' ? payload.volume : null);
                            if (volVal !== null && (Number(volVal) === 0 || Number(volVal) === 1)) {
                                if (Number(volVal) === 0) {
                                    await pressKey(25);
                                    try { sendToUser(user, packer.pack({ media: 'message', type: 'volume_set', payload: { ok: true, action: 'small-decrease' } })); } catch (e) { /* ignore */ }
                                } else {
                                    await pressKey(24);
                                    try { sendToUser(user, packer.pack({ media: 'message', type: 'volume_set', payload: { ok: true, action: 'small-increase' } })); } catch (e) { /* ignore */ }
                                }
                                return;
                            }
                        } catch (e) {
                            logger.debug('setVolume small-step handler error', e?.message || e);
                        }

                        if (action === 'up') {
                            await pressKey(24);
                            try { sendToUser(user, packer.pack({ media: 'message', type: 'volume_set', payload: { ok: true, action: 'up' } })); } catch (e) { /* ignore */ }
                            return;
                        }
                        if (action === 'down') {
                            await pressKey(25);
                            try { sendToUser(user, packer.pack({ media: 'message', type: 'volume_set', payload: { ok: true, action: 'down' } })); } catch (e) { /* ignore */ }
                            return;
                        }
                        if (action === 'mute' || action === 'toggle-mute') {
                            await pressKey(164);
                            try { sendToUser(user, packer.pack({ media: 'message', type: 'volume_set', payload: { ok: true, action: 'mute' } })); } catch (e) { /* ignore */ }
                            return;
                        }
                        if (typeof deltaSteps === 'number') {
                            const stepKey = deltaSteps > 0 ? 24 : 25;
                            const steps = Math.min(Math.abs(deltaSteps), 200);
                            for (let i = 0; i < steps; i++) {
                                try { await pressKey(stepKey); } catch (e) { logger.error('setVolume delta pressKey failed', e?.message || e); }
                                await new Promise((r) => setTimeout(r, 80));
                            }
                            try { sendToUser(user, packer.pack({ media: 'message', type: 'volume_set', payload: { ok: true, delta: deltaSteps } })); } catch (e) { /* ignore */ }
                            return;
                        }
                        if (typeof targetPercent === 'number') {
                            const assumedMax = 15;
                            const desiredIndex = Math.round((targetPercent / 100) * assumedMax);
                            const clampDowns = Math.min(assumedMax + 5, 40);
                            for (let i = 0; i < clampDowns; i++) {
                                try { await pressKey(25); } catch (e) { logger.error('setVolume clamp down pressKey failed', e?.message || e); }
                                await new Promise((r) => setTimeout(r, 60));
                            }
                            for (let i = 0; i < Math.min(desiredIndex, 60); i++) {
                                try { await pressKey(24); } catch (e) { logger.error('setVolume up pressKey failed', e?.message || e); }
                                await new Promise((r) => setTimeout(r, 80));
                            }
                            if (payload && payload.muted) {
                                try { await pressKey(164); } catch (e) { logger.error('setVolume mute pressKey failed', e?.message || e); }
                            }
                            try { sendToUser(user, packer.pack({ media: 'message', type: 'volume_set', payload: { ok: true, note: 'best-effort-applied', targetPercent } })); } catch (e) { /* ignore */ }
                            return;
                        }
                        try { sendToUser(user, packer.pack({ media: 'message', type: 'volume_set', payload: { ok: false, error: 'invalid-payload' } })); } catch (e) { /* ignore */ }
                    } catch (e) {
                        logger.error('setVolume handler error', e?.message || e);
                        try { sendToUser(user, packer.pack({ media: 'message', type: 'volume_set', payload: { error: String(e) } }), true); } catch (er) { /* ignore */ }
                    }
                })();
                return;
            }

            // ------------------------------------------------------------------
            // Standard controller commands
            // ------------------------------------------------------------------
            if (user?.client?.controller) {
                const controllerCmdAliases = {
                    injectPointer: 'injectTouch',
                    injectMouse: 'injectTouch',
                    pointer: 'injectTouch',
                };
                const cmd = controllerCmdAliases[incomingCmd] || incomingCmd;

                if (cmd === 'injectKeyCode') {
                    (async () => {
                        try {
                            const p = record.payload;
                            if (typeof p === 'string') {
                                const name = p.trim().toUpperCase();
                                const keyCode = KEY_NAME_MAP[name] || KEY_NAME_MAP[name.replace(/\s+/g, '_')];
                                if (keyCode) await pressKey(keyCode);
                                else logger.warn(`injectKeyCode: unknown key name '${p}'`);
                                return;
                            }
                            if (p && typeof p === 'object') {
                                if (typeof p.keyCode !== 'undefined' || typeof p.action !== 'undefined') {
                                    await safeInvokeController('injectKeyCode', p);
                                    return;
                                }
                                const nameField = p.key || p.keystroke || p.code || null;
                                if (nameField) {
                                    const name = String(nameField).trim().toUpperCase();
                                    const keyCode = KEY_NAME_MAP[name] || KEY_NAME_MAP[name.replace(/\s+/g, '_')];
                                    if (keyCode) await pressKey(keyCode);
                                    else logger.warn(`injectKeyCode: unknown key name in object: '${nameField}'`);
                                    return;
                                }
                            }
                            await safeInvokeController('injectKeyCode', p);
                        } catch (e) {
                            logger.error(`injectKeyCode handler error: ${e?.message || e}`);
                        }
                    })();

                } else if (cmd === 'injectTouch') {
                    if (Array.isArray(record.payload)) {
                        for (const ev of record.payload) safeInvokeController('injectTouch', ev);
                    } else {
                        safeInvokeController('injectTouch', record.payload);
                    }

                } else if (cmd === 'injectScroll') {
                    try {
                        logger.debug(`injectScroll received id=${id} payload=${safeStringify(record.payload)}`);
                    } catch (e) { /* ignore */ }
                    try {
                        const normalized = normalizeScrollPayload(user, record.payload);
                        if (normalized) {
                            try {
                                if (normalized.pointerId && typeof normalized.pointerId === 'bigint') {
                                    normalized.pointerId = Number(normalized.pointerId);
                                }
                            } catch (e) { logger.debug('injectScroll pointerId conversion failed', e?.message || e); }
                            logger.debug(`injectScroll normalized for id=${id}: ${safeStringify(normalized)}`);
                            safeInvokeController('injectScroll', normalized);
                        } else {
                            logger.debug(`injectScroll: normalization null for id=${id}, using raw`);
                            safeInvokeController('injectScroll', record.payload);
                        }
                    } catch (err) {
                        logger.error(`injectScroll failed for id=${id}: ${err?.message || err}`);
                        logger.debug(err?.stack || err);
                        try { ws.send(packer.pack({ media: 'error', message: `injectScroll failed: ${err?.message || String(err)}` }), true); } catch (e) { /* ignore */ }
                    }

                } else if (cmd === 'setScreenPowerMode') {
                    safeInvokeController('setScreenPowerMode', record.payload);
                } else if (cmd === 'rotateDevice') {
                    safeInvokeController('rotateDevice', record.payload);
                } else if (cmd === 'clipboardPaste') {
                    safeInvokeController('setClipboard', record.payload);
                }
            } else {
                logger.debug(`No client controller for id=${id}. cmd=${record?.cmd} payload=${safeStringify(record?.payload)}`);
            }
        } catch (ex) {
            logger.error(ex);
        }
    };
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function _makeSafeStringify() {
    return function safeStringify(obj) {
        const seen = new WeakSet();
        const serialize = (value) => {
            if (value === null) return null;
            const t = typeof value;
            if (t === 'bigint') return `${value.toString()}n`;
            if (t === 'function') return `[Function ${value.name || 'anonymous'}]`;
            if (t !== 'object') return value;
            if (value instanceof Uint8Array) return `Uint8Array(len=${value.length})`;
            if (typeof Buffer !== 'undefined' && value instanceof Buffer) return `Buffer(len=${value.length})`;
            if (seen.has(value)) return '[Circular]';
            seen.add(value);
            if (Array.isArray(value)) return value.map(serialize);
            const out = {};
            for (const k of Object.keys(value)) {
                try { out[k] = serialize(value[k]); } catch (e) { out[k] = `[Unserializable:${String(e)}]`; }
            }
            return out;
        };
        try {
            return JSON.stringify(serialize(obj), null, 2);
        } catch (e) {
            try { return String(obj); } catch (_) { return '[Unstringifiable]'; }
        }
    };
}

function _sanitizePointerIdFields(o) {
    if (!o || typeof o !== 'object') return;
    for (const [k, val] of Object.entries(o)) {
        if (k === 'pointerId' || k === 'pointer_id' || k === 'pid') {
            try {
                if (typeof val === 'bigint') { /* ok */ }
                else if (typeof val === 'number') o[k] = BigInt(Math.trunc(val));
                else if (typeof val === 'string') {
                    const s = val.trim().replace(/n$/i, '');
                    const num = Number(s);
                    o[k] = Number.isNaN(num) ? BigInt(-2) : BigInt(Math.trunc(num));
                } else { o[k] = BigInt(-2); }
            } catch (e) { o[k] = BigInt(-2); }
        } else if (val && typeof val === 'object') {
            _sanitizePointerIdFields(val);
        }
    }
}

function _convertNonPointerBigInt(o) {
    if (!o || typeof o !== 'object') return;
    for (const [k, val] of Object.entries(o)) {
        if (k === 'pointerId' || k === 'pointer_id' || k === 'pid') continue;
        if (typeof val === 'bigint') o[k] = Number(val);
        else if (val && typeof val === 'object') _convertNonPointerBigInt(val);
    }
}
