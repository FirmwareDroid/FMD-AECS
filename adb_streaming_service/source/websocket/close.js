/**
 * WebSocket `close` and `drain` handlers.
 *
 * @param {object} deps
 * @param {object} deps.logger
 * @param {object} deps.users              - global.users Map
 * @param {Function} deps.evictPushedSerial
 *
 * @returns {{ close: Function, drain: Function }}
 */
export function createCloseHandler({ logger, users, evictPushedSerial }) {
    return {
        drain(ws) {
            logger.info(`WebSocket backpressure: ${ws.getBufferedAmount()}`);
        },

        async close(ws, code, message) {
            logger.info(`WebSocket close event received code=${code} message=${JSON.stringify(message)}`);
            try {
                const { id } = ws;
                const user = users.get(id);

                if (user && (user._serverInitiatedClose || user._serverCloseReason)) {
                    logger.info(`WebSocket close (server-initiated) id=${id} reason=${user._serverCloseReason || '<none>'}`);
                    if (user._serverCloseStack) logger.debug(`Server close stack: ${user._serverCloseStack}`);
                } else {
                    logger.info(`WebSocket close (client-initiated or unknown) id=${id}`);
                }

                if (user) {
                    // Abort any active pipelines
                    if (user.abortController) {
                        try {
                            if (!user.abortController.signal.aborted) user.abortController.abort();
                        } catch (err) {
                            logger.debug('AbortController.abort() threw (ignored):', err?.message || err);
                        }
                        user.abortController = undefined;
                    }

                    // Evict the scrcpy-server push cache so the next session re-validates
                    try {
                        const serial = user.device?.serial || user.device?.original || user.device;
                        if (serial) evictPushedSerial(serial);
                    } catch (e) { /* ignore */ }

                    user.ws = null;
                    users.delete(id);

                    if (user.client) {
                        try {
                            await user.client.close();
                        } catch (err) {
                            logger.debug('user.client.close() error (ignored):', err?.message || err);
                        }
                    }
                }
            } catch (ex) {
                logger.error(ex);
            }
        },
    };
}
