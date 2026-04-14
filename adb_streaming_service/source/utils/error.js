/**
 * Serialize Error objects (including non-enumerable properties) into plain objects
 * so they can be safely packed/logged via msgpack or JSON.
 *
 * @param {*} err
 * @returns {object|null}
 */
export function serializeError(err) {
    if (!err) return null;
    try {
        const out = {
            name: err.name || 'Error',
            message: err.message || String(err),
            stack: err.stack || null,
        };
        // include other own properties (e.g., code, errno, cause)
        for (const k of Object.getOwnPropertyNames(err)) {
            if (k === 'name' || k === 'message' || k === 'stack') continue;
            try {
                out[k] = err[k];
            } catch (e) {
                out[k] = `unserializable(${String(e)})`;
            }
        }
        // if err has a cause that's an Error, include it serialized
        if (err.cause && typeof err.cause === 'object') {
            try {
                out.cause = serializeError(err.cause);
            } catch (e) {
                out.cause = String(err.cause);
            }
        }
        return out;
    } catch (e) {
        return { name: 'Error', message: String(err), stack: err && err.stack ? err.stack : null };
    }
}
