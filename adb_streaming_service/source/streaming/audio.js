/**
 * Audio streaming pipeline.
 *
 * Awaits the audioStream promise from the scrcpy client, handles the metadata
 * switch (disabled / errored / success), and pipes audio packets to the
 * connected WebSocket user.
 *
 * @param {object} deps - Injected dependencies.
 * @param {object} deps.packer    - msgpackr Packr instance.
 * @param {object} deps.logger    - pino logger instance.
 * @param {Function} deps.sendToUser  - (user, packed) → boolean
 * @param {Function} deps.closeSocket - (ws, reason, serverInitiated) → void
 * @param {Function} deps.serializeError - Error → plain object
 * @param {Function} deps.withTimeout    - (promise, ms, name) → Promise
 * @param {Function} deps.validateAudioPacket - (buf, metadata, id, ws) → {ok, reason, details}
 * @param {Function} deps.extractAudioBuffer  - (packet) → {buf, pktType, pktLen, info}
 *
 * @param {object} user  - Session user object.
 * @param {object} ws    - uWebSockets.js WebSocket (for codec info).
 * @param {object} client - scrcpy client object.
 * @param {string} id    - Session ID.
 */
export async function setupAudioStream(deps, user, ws, client, id) {
    const {
        packer, logger, sendToUser, closeSocket,
        serializeError, withTimeout,
        validateAudioPacket, extractAudioBuffer,
    } = deps;

    if (!client.audioStream) return;

    logger.info(`Setting up audio streaming for id='${id}' audio=${ws.audio}`);
    logger.info(`Using audioCodec='${ws.audioCodec}' audioEncoder='${ws.audioEncoder || '<default>'}'`);

    let metadata;
    try {
        metadata = await withTimeout(client.audioStream, 8000, 'audioStream').catch((err) => {
            logger.error(err?.message || err);
        });
    } catch (streamErr) {
        const errObj = serializeError(streamErr);
        logger.error(`audioStream creation failed for id=${id}: ${errObj.message}`);
        logger.debug('audioStream error details:', errObj);
        try {
            sendToUser(user, packer.pack({
                media: 'error',
                code: 'audio_stream_failed',
                message: `Failed to start audio stream: ${errObj.message}`,
                error: errObj,
            }), true);
        } catch (e) { /* ignore send errors */ }
        closeSocket(ws, `audioStream failed: ${errObj.message}`, true);
        return;
    }

    user.audioStats = user.audioStats || {
        sent: 0, dropped: 0, lastPacketSize: 0, lastPacketTs: null,
    };
    logger.info(`Audio stream metadata for id=${id}: type=${metadata?.type}`);
    logger.debug(
        'Audio metadata detail',
        metadata && typeof metadata === 'object'
            ? (metadata.stream ? Object.assign({}, metadata, { stream: '<stream>' }) : metadata)
            : String(metadata),
    );

    switch (metadata?.type) {
        case 'disabled':
            logger.info('AudioStream disabled');
            break;

        case 'errored':
            logger.error('AudioStream errored');
            try {
                sendToUser(user, packer.pack({ media: 'audio_error', message: 'audio stream errored' }));
            } catch (e) { /* ignore */ }
            break;

        case 'success': {
            const audioPacketStream = metadata.stream;
            if (!audioPacketStream) {
                logger.error('AudioStream success but stream is missing');
                try {
                    sendToUser(user, packer.pack({ media: 'audio_error', message: 'audio stream missing' }));
                } catch (e) { /* ignore */ }
                break;
            }

            // Notify client so it can prepare playback
            try {
                sendToUser(user, packer.pack({ media: 'audio_metadata', packet: metadata }));
            } catch (e) {
                logger.debug('Failed to send audio_metadata', e?.message || e);
            }

            // Per-stream abort controller (allows stopping audio without killing video)
            user.audioAbortController = user.audioAbortController || new AbortController();
            const audioSignal = user.audioAbortController.signal;

            // Wait for client to signal readiness
            try {
                const ready = await _waitForAudioReady(user, 2000);
                if (!ready) {
                    logger.debug(`Audio: client did not signal ready within timeout for id=${id}; continuing`);
                } else {
                    logger.debug(`Audio: client signalled ready for id=${id}`);
                }
            } catch (e) {
                logger.debug(`Audio readiness check failed for id=${id}: ${e?.message || e}`);
            }

            const DROP_ABORT_THRESHOLD =
                Number.isFinite(Number(process.env.AUDIO_DROP_ABORT_THRESHOLD))
                    ? Number(process.env.AUDIO_DROP_ABORT_THRESHOLD)
                    : 200;

            audioPacketStream.pipeTo(new WritableStream({
                write(packet) {
                    try {
                        // Unwrap common wrappers to obtain a raw binary buffer
                        let buf = null;
                        let pktType = typeof packet;
                        let pktLen = null;
                        let info = null;

                        try {
                            if (packet && typeof packet === 'object' && ('data' in packet)) {
                                const d = packet.data;
                                if (d instanceof Uint8Array || (typeof Buffer !== 'undefined' && Buffer.isBuffer(d))) {
                                    buf = d; pktType = 'Wrapped(data:Uint8Array)'; pktLen = d.length;
                                } else if (typeof ArrayBuffer !== 'undefined' && ArrayBuffer.isView && ArrayBuffer.isView(d)) {
                                    try {
                                        buf = new Uint8Array(d.buffer, d.byteOffset || 0, d.byteLength || d.buffer.byteLength);
                                        pktType = 'Wrapped(data:ArrayBufferView)'; pktLen = buf.length;
                                    } catch (e) { /* ignore */ }
                                } else if (typeof ArrayBuffer !== 'undefined' && d instanceof ArrayBuffer) {
                                    buf = new Uint8Array(d); pktType = 'Wrapped(data:ArrayBuffer)'; pktLen = buf.length;
                                } else if (typeof d === 'string') {
                                    const s = d.trim();
                                    if (s.length > 32 && /^[A-Za-z0-9+/=\s]+$/.test(s)) {
                                        try {
                                            const b = Buffer.from(s.replace(/\s+/g, ''), 'base64');
                                            if (b && b.length > 0) { buf = b; pktType = 'Wrapped(data:Base64String)'; pktLen = b.length; }
                                        } catch (e) { /* ignore */ }
                                    }
                                }
                                if (!buf) {
                                    const res = extractAudioBuffer(packet);
                                    buf = res.buf; pktType = res.pktType; pktLen = res.pktLen; info = res.info;
                                }
                            } else {
                                const res = extractAudioBuffer(packet);
                                buf = res.buf; pktType = res.pktType; pktLen = res.pktLen; info = res.info;
                            }
                        } catch (e) {
                            try {
                                const res = extractAudioBuffer(packet);
                                buf = res.buf; pktType = res.pktType; pktLen = res.pktLen; info = res.info;
                            } catch (ee) {
                                buf = null; pktType = typeof packet; pktLen = null;
                            }
                        }

                        const sendPacket = buf || packet;
                        const usedPktType = buf ? 'Uint8Array' : pktType;
                        const usedPktLen = buf ? pktLen : (packet && packet.length ? packet.length : null);

                        user.audioStats.sent += 1;
                        user.audioStats.lastPacketSize = usedPktLen || 0;
                        user.audioStats.lastPacketTs = Date.now();

                        // Codec-specific validation
                        try {
                            const validation = validateAudioPacket(buf, metadata || {}, id, user);
                            if (!validation.ok) {
                                logger.warn(`Audio validation failed for id=${id}: ${validation.reason}`, validation.details || {});
                                user.audioStats.dropped += 1;
                                try {
                                    sendToUser(user, packer.pack({
                                        media: 'audio_invalid',
                                        reason: validation.reason,
                                        details: validation.details,
                                    }));
                                } catch (e) {
                                    logger.debug('Failed to notify user about invalid audio packet', e?.message || e);
                                }
                                return;
                            }
                        } catch (e) {
                            logger.debug('Audio validation threw exception', e?.message || e);
                        }

                        // Debug logging for unexpected packet shapes
                        if (!buf || usedPktLen === 0) {
                            logger.warn(`Audio packet with unexpected shape/type id=${id} type=${usedPktType} pktLen=${usedPktLen} sent=${user.audioStats.sent}`);
                            try {
                                if (buf && buf.slice) {
                                    const preview = buf.slice(0, 8);
                                    logger.debug('Audio packet preview (hex)', Array.from(preview).map((b) => b.toString(16).padStart(2, '0')).join(' '));
                                } else if (info && info.keys) {
                                    logger.debug('Audio packet wrapper info keys', info.keys);
                                    try { logger.debug('Audio packet keys preview', JSON.stringify(Object.keys(packet).slice(0, 20))); } catch (e) { /* ignore */ }
                                } else {
                                    try { logger.debug('Audio packet content preview', JSON.stringify(packet).slice(0, 200)); } catch (e) { /* ignore */ }
                                }
                            } catch (e) {
                                logger.debug('Failed to preview audio packet', e?.message || e);
                            }
                        }

                        // Send packet and track backpressure
                        const beforeBuffered = (user.ws && typeof user.ws.getBufferedAmount === 'function') ? user.ws.getBufferedAmount() : null;
                        const packed = packer.pack({ media: 'audio', packet: sendPacket });
                        const ok = sendToUser(user, packed);

                        if (!ok) {
                            user.audioStats.dropped += 1;
                            logger.debug(`Audio packet dropped id=${id} sent=${user.audioStats.sent} dropped=${user.audioStats.dropped} bufBefore=${beforeBuffered}`);
                        } else if (user.audioStats.sent % 50 === 0) {
                            const afterBuffered = (user.ws && typeof user.ws.getBufferedAmount === 'function') ? user.ws.getBufferedAmount() : null;
                            logger.debug(`Audio packets sent id=${id} count=${user.audioStats.sent} lastSize=${user.audioStats.lastPacketSize} dropped=${user.audioStats.dropped} bufBefore=${beforeBuffered} bufAfter=${afterBuffered}`);
                        }

                        // Abort audio stream when too many consecutive drops
                        if (user.audioStats.dropped >= DROP_ABORT_THRESHOLD) {
                            logger.warn(`Audio too many drops for id=${id} (${user.audioStats.dropped}), aborting audio stream`);
                            try {
                                sendToUser(user, packer.pack({
                                    media: 'audio_backpressure',
                                    message: 'server aborting audio stream due to backpressure',
                                    dropped: user.audioStats.dropped,
                                }));
                            } catch (e) {
                                logger.debug('Failed to send audio_backpressure', e?.message || e);
                            }
                            try {
                                if (user.audioAbortController && !user.audioAbortController.signal.aborted) {
                                    user.audioAbortController.abort();
                                }
                            } catch (e) {
                                logger.debug('Failed to abort audioAbortController', e?.message || e);
                            }
                        }
                    } catch (ex) {
                        user.audioStats.dropped += 1;
                        logger.error(`Error sending audio packet id=${id} sent=${user.audioStats.sent} dropped=${user.audioStats.dropped}: ${ex?.message || ex}`);
                        try {
                            sendToUser(user, packer.pack({ media: 'audio_error', message: String(ex) }));
                        } catch (e) { /* ignore */ }
                    }
                },
            }), { signal: audioSignal }).catch((e) => {
                if (audioSignal && audioSignal.aborted) {
                    logger.info(`Audio stream aborted for id=${id}`);
                } else if (user?.abortController?.signal?.aborted) {
                    logger.info(`Audio stream stopped because connection aborted for id=${id}`);
                } else {
                    logger.error('audioPacketStream pipe error', e);
                }
            });
            break;
        }

        default:
            logger.debug('Unknown audio metadata.type', metadata && metadata.type);
            break;
    }
}

/**
 * Waits up to `timeoutMs` for `user.audioReady` to become truthy.
 * @param {object} user
 * @param {number} timeoutMs
 * @returns {Promise<boolean>}
 */
function _waitForAudioReady(user, timeoutMs = 2000) {
    if (!user) return Promise.resolve(false);
    if (user.audioReady) return Promise.resolve(true);
    return new Promise((resolve) => {
        let resolved = false;
        const to = setTimeout(() => {
            if (!resolved) { resolved = true; resolve(false); }
        }, timeoutMs);
        const check = () => {
            if (user.audioReady && !resolved) {
                resolved = true;
                clearTimeout(to);
                resolve(true);
            } else if (!resolved) {
                setTimeout(check, 50);
            }
        };
        check();
    });
}
