import { logger } from '../../services/logger.js';

/**
 * Normalize a touch payload into the ScrcpyInjectTouchControlMessage shape.
 *
 * @param {object} user   - Session user object (may carry videoMetadata).
 * @param {object} payload - Raw client payload.
 * @returns {Promise<object|null>}
 */
export async function normalizeTouchPayload(user, payload) {
    try {
        if (!payload || typeof payload !== 'object') return null;

        const deviceX = payload.deviceX ?? payload.device_x ?? null;
        const deviceY = payload.deviceY ?? payload.device_y ?? null;
        const rotation = (typeof payload.rotation !== 'undefined') ? Number(payload.rotation)
            : (typeof payload.rot !== 'undefined' ? Number(payload.rot) : null);

        let x = deviceX ?? payload.x ?? payload.clientX ?? payload.cx ?? payload.posX ?? payload.pointerX ?? payload.pointer_x ?? payload.screenX ?? payload.screen_x;
        let y = deviceY ?? payload.y ?? payload.clientY ?? payload.cy ?? payload.posY ?? payload.pointerY ?? payload.pointer_y ?? payload.screenY ?? payload.screen_y;

        let action = payload.action ?? payload.type ?? payload.event ?? payload.actionButton ?? payload.actionType ?? null;
        let pointerId = payload.pointerId ?? payload.pointer_id ?? payload.pid ?? payload.pointer ?? null;
        let pressure = payload.pressure ?? payload.p ?? payload.force ?? 0.5;
        let buttons = payload.buttons ?? payload.button ?? 1;

        let videoWidth = payload.screenWidth ?? payload.screen_width ?? payload.displayWidth ?? payload.display_width
            ?? payload.deviceWidth ?? payload.device_width ?? payload.displayW ?? payload.width ?? payload.w
            ?? payload.videoWidth ?? payload.video_width ?? null;
        let videoHeight = payload.screenHeight ?? payload.screen_height ?? payload.displayHeight ?? payload.display_height
            ?? payload.deviceHeight ?? payload.device_height ?? payload.displayH ?? payload.height ?? payload.h
            ?? payload.videoHeight ?? payload.video_height ?? null;

        if (typeof action === 'string') {
            const a = action.toLowerCase();
            if (a.includes('down')) action = 0;
            else if (a.includes('up')) action = 1;
            else if (a.includes('move')) action = 2;
            else action = Number(action) || 0;
        } else { action = Number(action) || 0; }

        if (pointerId === null || typeof pointerId === 'undefined') {
            pointerId = (payload && typeof payload.source === 'string' && payload.source.toLowerCase().includes('mouse')) ? -1n : -2n;
        } else {
            try {
                if (typeof pointerId === 'bigint') { /* ok */ }
                else if (typeof pointerId === 'number') pointerId = BigInt(Math.trunc(pointerId));
                else if (typeof pointerId === 'string') {
                    const s = pointerId.trim().replace(/n$/i, '');
                    const num = Number(s);
                    pointerId = Number.isNaN(num) ? BigInt(-2) : BigInt(Math.trunc(num));
                } else { pointerId = BigInt(-2); }
            } catch (e) { pointerId = BigInt(-2); }
        }

        pressure = Number(pressure) || 0.5;
        buttons = Number(buttons) || 1;
        x = typeof x === 'string' ? Number(x) : x;
        y = typeof y === 'string' ? Number(y) : y;
        x = Number(x); y = Number(y);
        if (!isFinite(x)) x = NaN;
        if (!isFinite(y)) y = NaN;

        if ((!videoWidth || !videoHeight) || Number(videoWidth) === 0 || Number(videoHeight) === 0) {
            try {
                if (user && user.videoMetadata) {
                    const vs = user.videoMetadata;
                    videoWidth = videoWidth || vs.width || vs.codec_width || vs.display_width || vs.displayWidth || null;
                    videoHeight = videoHeight || vs.height || vs.codec_height || vs.display_height || vs.displayHeight || null;
                } else if (user && user.client && user.client.videoStream) {
                    const vsObj = await user.client.videoStream;
                    if (vsObj && vsObj.metadata) {
                        videoWidth = videoWidth || vsObj.metadata.width || vsObj.metadata.codec_width || null;
                        videoHeight = videoHeight || vsObj.metadata.height || vsObj.metadata.codec_height || null;
                    }
                }
            } catch (e) { /* ignore */ }
        }

        logger.debug('normalizeTouchPayload debug', {
            deviceX, deviceY, x, y, videoWidth, videoHeight,
            coordsAreNormGuess: (isFinite(x) && isFinite(y) && Math.abs(x) <= 1 && Math.abs(y) <= 1 && deviceX == null && deviceY == null),
        });

        const coordsAreNormalized = isFinite(x) && isFinite(y) && Math.abs(x) <= 1 && Math.abs(y) <= 1
            && deviceX == null && deviceY == null;

        if (coordsAreNormalized) {
            if (!isFinite(Number(videoWidth)) || !isFinite(Number(videoHeight)) || Number(videoWidth) <= 0 || Number(videoHeight) <= 0) {
                if (Number(x) === 0 && Number(y) === 0) { videoWidth = 1; videoHeight = 1; }
                else { logger.debug('normalizeTouchPayload returning null: normalized coords but missing video size'); return null; }
            }
        } else {
            if (!isFinite(Number(videoWidth)) || !isFinite(Number(videoHeight)) || Number(videoWidth) <= 0 || Number(videoHeight) <= 0) {
                videoWidth = 1; videoHeight = 1;
            }
        }

        videoWidth = Number(videoWidth);
        videoHeight = Number(videoHeight);

        if (coordsAreNormalized) { x = Math.round(x * videoWidth); y = Math.round(y * videoHeight); }
        else { if (!isFinite(x)) x = 0; if (!isFinite(y)) y = 0; x = Math.round(x); y = Math.round(y); }

        if (rotation != null && deviceX == null && deviceY == null) {
            const rot = Number(rotation) % 4;
            if (rot !== 0) {
                let nx = x; let ny = y;
                if (rot === 1) { nx = y; ny = videoWidth - x - 1; }
                else if (rot === 2) { nx = videoWidth - x - 1; ny = videoHeight - y - 1; }
                else if (rot === 3) { nx = videoHeight - y - 1; ny = x; }
                if (rot === 1 || rot === 3) { const tmp = videoWidth; videoWidth = videoHeight; videoHeight = tmp; }
                x = Math.round(nx); y = Math.round(ny);
            }
        }

        return {
            action: Number(action),
            pointerId,
            pointerX: Number(x),
            pointerY: Number(y),
            videoWidth: Number(videoWidth),
            videoHeight: Number(videoHeight),
            pressure: Number(pressure),
            buttons: Number(buttons),
        };
    } catch (err) {
        logger.error('normalizeTouchPayload unexpected error', err);
        return null;
    }
}

/**
 * Last-resort fallback normalizer.
 *
 * @param {object} user
 * @param {object} payload
 * @returns {object|null}
 */
export function fallbackNormalizePayload(user, payload) {
    try {
        if (!payload || typeof payload !== 'object') return null;
        let x = payload.deviceX ?? payload.device_x ?? payload.pointerX ?? payload.pointer_x ?? payload.x ?? payload.clientX ?? payload.cx ?? null;
        let y = payload.deviceY ?? payload.device_y ?? payload.pointerY ?? payload.pointer_y ?? payload.y ?? payload.clientY ?? payload.cy ?? null;
        let videoW = payload.screenWidth ?? payload.screen_width ?? payload.displayWidth ?? payload.display_width ?? (user && user.videoMetadata ? user.videoMetadata.width : null) ?? 1;
        let videoH = payload.screenHeight ?? payload.screen_height ?? payload.displayHeight ?? payload.display_height ?? (user && user.videoMetadata ? user.videoMetadata.height : null) ?? 1;
        x = typeof x === 'string' ? Number(x) : x;
        y = typeof y === 'string' ? Number(y) : y;
        if (!isFinite(x) || !isFinite(y)) return null;
        if (Math.abs(x) <= 1 && Math.abs(y) <= 1 && videoW > 1 && videoH > 1) {
            x = Math.round(x * videoW); y = Math.round(y * videoH);
        } else { x = Math.round(x); y = Math.round(y); }
        let pointerId = payload.pointerId ?? payload.pointer_id ?? payload.pid ?? payload.pointer ?? -2;
        try {
            if (typeof pointerId === 'string') {
                const s = pointerId.trim().replace(/n$/i, '');
                const num = Number(s);
                pointerId = Number.isNaN(num) ? -2 : Math.trunc(num);
            } else if (typeof pointerId === 'number') { pointerId = Math.trunc(pointerId); }
            else if (typeof pointerId === 'bigint') { pointerId = Number(pointerId); }
            else { pointerId = -2; }
        } catch (e) { pointerId = -2; }
        return {
            action: Number(payload.action ?? payload.type ?? 0),
            pointerId: Number(pointerId),
            pointerX: x, pointerY: y,
            videoWidth: Number(videoW), videoHeight: Number(videoH),
            pressure: Number(payload.pressure ?? payload.p ?? 0.5) || 0.5,
            buttons: Number(payload.buttons ?? payload.button ?? 1) || 1,
        };
    } catch (err) {
        logger.debug('fallbackNormalizePayload unexpected error', err && err.message ? err.message : err);
        return null;
    }
}
