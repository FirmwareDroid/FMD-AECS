/**
 * WebSocket `open` handler.
 *
 * Handles a new WebSocket connection: resolves the device, starts the scrcpy
 * client, sets up stdout/clipboard piping, and kicks off audio + video streams.
 *
 * @param {object} deps - Injected dependencies.
 * @param {object} deps.packer
 * @param {object} deps.logger
 * @param {object} deps.users              - global.users Map
 * @param {object} deps.adbTcpService
 * @param {Function} deps.sendToUser
 * @param {Function} deps.closeSocket
 * @param {Function} deps.serializeError
 * @param {Function} deps.setupAudioStream
 * @param {Function} deps.setupVideoStream
 *
 * @returns {Function} uWebSockets.js open callback.
 */
export function createOpenHandler(deps) {
    const {
        packer, logger, users, adbTcpService,
        sendToUser, closeSocket, serializeError,
        setupAudioStream, setupVideoStream,
    } = deps;

    return async function open(ws) {
        logger.info('WebSocket connection opened');
        try {
            const { id } = ws;
            let device = ws.device;

            if (!id) {
                logger.error('WebSocket open: missing id query parameter');
                try { ws.send(packer.pack({ media: 'error', message: 'missing id' }), true); } catch (e) { /* ignore */ }
                closeSocket(ws, 'missing id', true);
                return;
            }

            // Auto-select device when none provided and exactly one is connected
            if (!device) {
                logger.info('No device specified, checking for auto-selection…');
                try {
                    const devices = await adbTcpService.getDevices();
                    if (Array.isArray(devices) && devices.length === 1) {
                        device = devices[0].serial || devices[0].id || devices[0];
                        logger.info(`Auto-selected single connected device: ${device}`);
                    } else {
                        try {
                            ws.send(packer.pack({
                                media: 'error',
                                message: 'missing device and multiple/zero devices connected',
                            }), true);
                        } catch (e) { /* ignore */ }
                        return;
                    }
                } catch (e) {
                    logger.error('Failed to list devices for auto-selection:', e);
                    try {
                        ws.send(packer.pack({
                            media: 'error',
                            message: 'missing device and could not list devices',
                        }), true);
                    } catch (e) { /* ignore */ }
                    return;
                }
            }

            const rawDeviceQuery = device;

            // Build an enriched device object from the query string value
            // Supported forms: "host:port/serial", "serial"
            let deviceObj = null;
            try {
                if (typeof rawDeviceQuery === 'string' && rawDeviceQuery.includes('/')) {
                    const [hostPort, serialPart] = rawDeviceQuery.split('/', 2);
                    const hpParts = hostPort.split(':');
                    const hostPart = hpParts[0] || 'localhost';
                    const portPart = hpParts[1] ? Number(hpParts[1]) : 5037;
                    deviceObj = {
                        original: rawDeviceQuery,
                        host: hostPart,
                        port: portPart,
                        serial: serialPart,
                        key: `${hostPart}:${portPart}`,
                        uniqueName: `${hostPart}:${portPart}/${serialPart}`,
                    };
                } else if (typeof rawDeviceQuery === 'string') {
                    deviceObj = { original: rawDeviceQuery, serial: rawDeviceQuery };
                } else if (rawDeviceQuery && typeof rawDeviceQuery === 'object') {
                    deviceObj = Object.assign({}, rawDeviceQuery);
                }
            } catch (e) {
                logger.debug('Failed to build deviceObj from query param', e?.message || e);
                deviceObj = {
                    original: rawDeviceQuery,
                    serial: typeof rawDeviceQuery === 'string' ? rawDeviceQuery : null,
                };
            }

            const deviceLabel = deviceObj?.uniqueName ?? deviceObj?.serial ?? device;
            logger.info(`WebSocket open: id='${id}' device='${deviceLabel}'`);

            const user = { ws, client: null, abortController: new AbortController() };
            users.set(id, user);
            logger.info(`Starting ADB TCP streaming for id='${id}' device='${deviceObj?.serial ?? '<unknown>'}' audio=${ws.audio} video=${ws.video}`);

            // Get device ADB client
            let deviceAdb;
            try {
                deviceAdb = await adbTcpService.getDeviceAdb(deviceObj);
            } catch (err) {
                logger.error(`Failed to get device adb for device='${deviceObj?.serial ?? device}' id='${id}':`, err);
                try {
                    ws.send(packer.pack({ media: 'error', message: `device '${deviceObj?.serial ?? device}' not connected` }), true);
                } catch (e) { /* ignore */ }
                users.delete(id);
                return;
            }

            // Start scrcpy client
            let startResult;
            try {
                logger.info(`Starting ADB TCP service for device='${device}' id='${id}'…`);
                startResult = await adbTcpService.start(deviceAdb, user);
            } catch (err) {
                const errObj = serializeError(err);
                logger.error(`Failed to start scrcpy client for device='${device}' id='${id}': ${errObj.message}`);
                logger.debug('Failed to start scrcpy client error details:', errObj);
                try {
                    ws.send(packer.pack({ media: 'error', message: 'failed to start client', error: errObj }), true);
                } catch (e) { /* ignore */ }
                users.delete(id);
                return;
            }

            const { client, options } = startResult || {};
            if (!client) {
                logger.error('No ADB TCP CLIENT');
                try { ws.send(packer.pack({ media: 'error', message: 'no client' }), true); } catch (e) { /* ignore */ }
                users.delete(id);
                return;
            }

            user.client = client;
            try { user.deviceAdb = deviceAdb; user.deviceSerial = device; } catch (e) {
                logger.debug('Could not set user.deviceAdb/serial', e?.message || e);
            }

            // Pipe client stdout to logger
            logger.info(`Setting up client.stdout piping for id='${id}'`);
            if (client.stdout && typeof client.stdout.pipeTo === 'function') {
                try {
                    client.stdout.pipeTo(
                        new WritableStream({ write: (line) => logger.info(line) }),
                        { signal: user.abortController?.signal || undefined },
                    ).catch((e) => {
                        if (user?.abortController?.signal?.aborted) return;
                        logger.error('Error piping client.stdout:', e);
                    });
                } catch (e) {
                    logger.error('Exception while piping client.stdout:', e);
                }
            } else {
                logger.debug('client.stdout not available, skipping stdout piping');
            }

            // Pipe clipboard events to client
            if (options && options.clipboard) {
                logger.info(`Setting up clipboard piping for id='${id}'`);
                options.clipboard.pipeTo(new WritableStream({
                    write: (message) => {
                        try { sendToUser(user, packer.pack({ media: 'message', message })); } catch (ex) {
                            logger.error(ex);
                        }
                    },
                }), { signal: user.abortController?.signal || undefined }).catch((e) => {
                    if (user?.abortController?.signal?.aborted) return;
                    logger.error(e);
                });
            }

            // Set up audio and video streams concurrently
            const streamDeps = {
                packer, logger, sendToUser, closeSocket, serializeError,
                withTimeout: deps.withTimeout, validateAudioPacket: deps.validateAudioPacket,
                extractAudioBuffer: deps.extractAudioBuffer,
            };
            await Promise.all([
                setupAudioStream(streamDeps, user, ws, client, id),
                setupVideoStream(streamDeps, user, ws, client, id),
            ]);
        } catch (err) {
            logger.error(err);
            try { ws.send(packer.pack({ media: 'error', message: 'internal server error' }), true); } catch (e) { /* ignore */ }
            if (ws.id) users.delete(ws.id);
            closeSocket(ws, 'internal server error', true);
        }
    };
}
