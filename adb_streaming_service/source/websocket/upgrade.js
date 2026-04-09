/**
 * WebSocket upgrade handler.
 *
 * Validates authentication and promotes the HTTP request to a WebSocket
 * connection, passing query parameters as user data on the ws object.
 *
 * @param {object} deps
 * @param {Function} deps.requireAuthForUpgrade
 *
 * @returns {Function} uWebSockets.js upgrade callback.
 */
export function createUpgradeHandler({ requireAuthForUpgrade }) {
    return async function upgrade(res, req, context) {
        if (!requireAuthForUpgrade(res, req)) return;
        res.upgrade(
            {
                url: req.getUrl(),
                id: req.getQuery('id'),
                device: req.getQuery('device'),
                audio: ['true', null, undefined].includes(req.getQuery('audio')),
                audioCodec: req.getQuery('audioCodec') ?? 'raw',
                audioEncoder: req.getQuery('audioEncoder') ?? undefined,
                video: ['true', null, undefined].includes(req.getQuery('video')),
                videoCodec: req.getQuery('videoCodec') ?? 'h264',
                videoEncoder: req.getQuery('videoEncoder') ?? undefined,
                videoBitRate: ![null, undefined].includes(req.getQuery('videoBitRate'))
                    ? Number.parseInt(req.getQuery('videoBitRate')) * 1_000_000
                    : 4_000_000,
                displayId: ![null, undefined].includes(req.getQuery('displayId'))
                    ? Number.parseInt(req.getQuery('displayId'))
                    : 0,
                maxSize: ![null, undefined].includes(req.getQuery('maxSize'))
                    ? Number.parseInt(req.getQuery('maxSize'))
                    : 1280,
                maxFps: ![null, undefined].includes(req.getQuery('maxFps'))
                    ? Number.parseInt(req.getQuery('maxFps'))
                    : 60,
            },
            req.getHeader('sec-websocket-key'),
            req.getHeader('sec-websocket-protocol'),
            req.getHeader('sec-websocket-extensions'),
            context,
        );
    };
}
