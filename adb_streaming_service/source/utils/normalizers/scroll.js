import { logger } from '../../services/logger.js';

/**
 * Normalize a scroll payload into the scrcpy expected shape.
 * scrollX/scrollY are clamped floats in [-1, 1].
 *
 * @param {object} user
 * @param {object} payload
 * @returns {object|null}
 */
export function normalizeScrollPayload(user, payload) {
    try {
        if (!payload || typeof payload !== 'object') return null;

        const deviceX = payload.deviceX ?? payload.device_x ?? null;
        const deviceY = payload.deviceY ?? payload.device_y ?? null;

        let x = deviceX ?? payload.pointerX ?? payload.clientX ?? payload.client_x ?? payload.x ?? null;
        let y = deviceY ?? payload.pointerY ?? payload.clientY ?? payload.client_y ?? payload.y ?? null;

        let videoWidth = payload.displayWidth ?? payload.display_width ?? payload.screenWidth ?? payload.screen_width
            ?? (user && user.videoMetadata ? user.videoMetadata.width : null) ?? 1;
        let videoHeight = payload.displayHeight ?? payload.display_height ?? payload.screenHeight ?? payload.screen_height
            ?? (user && user.videoMetadata ? user.videoMetadata.height : null) ?? 1;

        x = typeof x === 'string' ? Number(x) : x;
        y = typeof y === 'string' ? Number(y) : y;
        if (!isFinite(Number(x))) x = NaN;
        if (!isFinite(Number(y))) y = NaN;

        if ((x === null || Number.isNaN(x)) && (payload.clientX || payload.client_x)) {
            const cx = payload.clientX ?? payload.client_x;
            const cy = payload.clientY ?? payload.client_y;
            if (typeof cx === 'number' && typeof cy === 'number' && user && user.videoMetadata) {
                const metaW = user.clientWidth || user.videoMetadata.width || videoWidth;
                const metaH = user.clientHeight || user.videoMetadata.height || videoHeight;
                if (metaW > 0 && metaH > 0) {
                    x = Math.round(cx * (videoWidth / metaW));
                    y = Math.round(cy * (videoHeight / metaH));
                }
            }
        }

        if (!isFinite(x)) x = 0;
        if (!isFinite(y)) y = 0;
        x = Math.round(x); y = Math.round(y);

        let pointerId = payload.pointerId ?? payload.pointer_id ?? payload.pid ?? payload.pointer ?? -2n;
        try {
            if (pointerId === null || typeof pointerId === 'undefined') pointerId = -2n;
            else if (typeof pointerId === 'bigint') { /* ok */ }
            else if (typeof pointerId === 'number') pointerId = BigInt(Math.trunc(pointerId));
            else if (typeof pointerId === 'string') {
                const s = pointerId.trim().replace(/n$/i, '');
                const num = Number(s);
                pointerId = Number.isNaN(num) ? BigInt(-2) : BigInt(Math.trunc(num));
            } else pointerId = BigInt(-2);
        } catch (e) { pointerId = BigInt(-2); }

        let scrollX = payload.scrollX ?? payload.scroll_x ?? payload.deltaX ?? payload.delta_x ?? payload.amountX ?? payload.amount_x ?? 0;
        let scrollY = payload.scrollY ?? payload.scroll_y ?? payload.deltaY ?? payload.delta_y ?? payload.amountY ?? payload.amount_y ?? 0;
        scrollX = typeof scrollX === 'string' ? Number(scrollX) : scrollX;
        scrollY = typeof scrollY === 'string' ? Number(scrollY) : scrollY;
        if (!isFinite(Number(scrollX))) scrollX = 0;
        if (!isFinite(Number(scrollY))) scrollY = 0;

        videoWidth = Number(videoWidth) || 1;
        videoHeight = Number(videoHeight) || 1;
        const normX = videoWidth > 0 ? scrollX / videoWidth : 0;
        const normY = videoHeight > 0 ? scrollY / videoHeight : 0;
        const clamp = (v) => Math.max(-1, Math.min(1, v));
        // Normalise IEEE-754 negative-zero (-0) to plain 0
        const normalizeZero = (v) => v + 0;
        const finalScrollX = normalizeZero(Number(clamp(-normX)));
        const finalScrollY = normalizeZero(Number(clamp(-normY)));

        return {
            pointerId,
            pointerX: Number(x), pointerY: Number(y),
            x: Number(x), y: Number(y),
            scrollX: finalScrollX, scrollY: finalScrollY,
            scroll_x: finalScrollX, scroll_y: finalScrollY,
            hscroll: finalScrollX, vscroll: finalScrollY,
            deltaX: Number(scrollX) || 0, deltaY: Number(scrollY) || 0,
            buttons: Number(payload.buttons ?? payload.button ?? 0) || 0,
            action: Number(payload.action ?? payload.type ?? 0) || 0,
        };
    } catch (e) {
        try { logger.debug('normalizeScrollPayload error', e?.message || e); } catch (ex) { /* ignore */ }
        return null;
    }
}
