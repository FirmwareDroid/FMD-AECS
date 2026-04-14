import { logger } from '../../services/logger.js';
import { global } from '../../state/global.js';
import { requireAuthForHttp } from '../../utils/auth.js';

/**
 * GET /_debug/audio_stats
 *
 * Returns per-session audio statistics as JSON.
 * Protected by HTTP Basic auth when AUTH_ENABLED=true.
 */
export function audioStatsRoute(res, req) {
    if (!requireAuthForHttp(res, req)) return;
    try {
        const stats = {};
        for (const [id, u] of global.users.entries()) {
            stats[id] = {
                audioStats: u.audioStats || { sent: 0, dropped: 0, lastPacketSize: 0, lastPacketTs: null },
                connected: !!(u && u.client),
                device: u?.client?.device || null,
            };
        }
        res.writeStatus('200 OK');
        res.writeHeader('Content-Type', 'application/json; charset=utf-8');
        res.end(JSON.stringify(stats));
    } catch (e) {
        logger.error('Error in /_debug/audio_stats', e?.message || e);
        res.writeStatus('500 Internal Server Error');
        res.end('error');
    }
}
