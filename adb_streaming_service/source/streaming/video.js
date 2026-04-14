/**
 * Video streaming pipeline.
 *
 * Awaits the videoStream promise from the scrcpy client, sends the metadata
 * packet to the client, and pipes video frames to the connected WebSocket user.
 *
 * @param {object} deps - Injected dependencies.
 * @param {object} deps.packer    - msgpackr Packr instance.
 * @param {object} deps.logger    - pino logger instance.
 * @param {Function} deps.sendToUser  - (user, packed) → boolean
 * @param {Function} deps.closeSocket - (ws, reason, serverInitiated) → void
 * @param {Function} deps.serializeError - Error → plain object
 * @param {Function} deps.withTimeout    - (promise, ms, name) → Promise
 *
 * @param {object} user   - Session user object.
 * @param {object} ws     - uWebSockets.js WebSocket.
 * @param {object} client - scrcpy client object.
 * @param {string} id     - Session ID.
 */
export async function setupVideoStream(deps, user, ws, client, id) {
    const { packer, logger, sendToUser, closeSocket, serializeError, withTimeout } = deps;

    if (!client.videoStream) return;

    logger.info(`Setting up video streaming for id='${id}'`);

    let videoObj;
    try {
        videoObj = await withTimeout(client.videoStream, 10000, 'videoStream');
    } catch (streamErr) {
        const errObj = serializeError(streamErr);
        logger.error(`videoStream creation failed for id=${id}: ${errObj.message}`);
        logger.debug('videoStream error details:', errObj);
        try {
            sendToUser(user, packer.pack({
                media: 'error',
                code: 'video_stream_failed',
                message: `Failed to start video stream: ${errObj.message}`,
                error: errObj,
            }), true);
        } catch (e) { /* ignore send errors */ }
        closeSocket(ws, `videoStream failed: ${errObj.message}`, true);
        return;
    }

    const { metadata: videoMetadata, stream: videoPacketStream } = videoObj || {};
    logger.info(videoMetadata);

    // Store metadata on user so touch normalization can use display dimensions
    try { user.videoMetadata = videoMetadata; } catch (e) {
        logger.debug('Could not store videoMetadata on user', e?.message || e);
    }

    try {
        sendToUser(user, packer.pack({ media: 'video_metadata', packet: videoMetadata }));
    } catch (e) {
        logger.debug('Failed to send video_metadata', e?.message || e);
    }

    if (!videoPacketStream) {
        logger.warn('videoStream provided no stream object');
        return;
    }

    try {
        videoPacketStream.pipeTo(new WritableStream({
            write(packet) {
                try {
                    sendToUser(user, packer.pack({ media: 'video', packet }));
                } catch (ex) {
                    logger.error('Error sending video packet to ws:', ex?.message || ex);
                }
            },
        }), { signal: user.abortController?.signal || undefined }).catch((e) => {
            if (user?.abortController?.signal?.aborted) return;
            const errObj = serializeError(e);
            logger.error('videoPacketStream pipe error', errObj);
            try {
                sendToUser(user, packer.pack({
                    media: 'error',
                    code: 'video_pipe_error',
                    message: 'videoPacketStream pipe error',
                    error: errObj,
                }), true);
            } catch (se) { /* ignore */ }
            closeSocket(ws, 'videoPacketStream pipe error', true);
        });
    } catch (e) {
        const errObj = serializeError(e);
        logger.error('Exception while setting up video stream piping', errObj);
        try {
            sendToUser(user, packer.pack({
                media: 'error',
                code: 'video_setup_failed',
                message: 'Exception while setting up video stream',
                error: errObj,
            }), true);
        } catch (se) { /* ignore */ }
        closeSocket(ws, 'video setup failed', true);
    }
}
