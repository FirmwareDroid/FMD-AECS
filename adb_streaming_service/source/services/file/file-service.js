import { fileURLToPath } from "node:url";
import { dirname, join, basename } from "node:path";
import fs from "node:fs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const uploadsPath = join(__dirname, "..", "..", "uploads");
const appsPath = join(__dirname, "..", "..", "apps/sn-apps");

const FILE_EXTENSIONS = ['apk', 'apkm', 'xapk'];

// Default: 500 MB.  Set UPLOAD_MAX_BYTES in env to override.
const UPLOAD_MAX_BYTES = Number(process.env.UPLOAD_MAX_BYTES) || 500 * 1024 * 1024;

/**
 * Sanitize a filename:
 *  - strip path separators and null bytes
 *  - limit to the basename only
 *  - reject if extension is not in the allowed list
 *
 * @param {string} filename
 * @returns {string} sanitized filename
 * @throws {Error} when the filename is invalid or has a disallowed extension
 */
function sanitizeFilename(filename) {
    if (!filename || typeof filename !== 'string') throw new Error('Missing filename');
    // Strip path separators and null bytes
    let name = basename(filename.replace(/\0/g, ''));
    // Reject empty names or names that are only dots
    if (!name || /^\.+$/.test(name)) throw new Error('Invalid filename');
    // Validate extension
    const ext = name.split('.').pop()?.toLowerCase();
    if (!ext || !FILE_EXTENSIONS.includes(ext)) {
        throw new Error(`Disallowed file extension: .${ext}. Allowed: ${FILE_EXTENSIONS.join(', ')}`);
    }
    return name;
}

class FileService {
	async upload(body) {
		const promises = [];

		await new Promise((resolve, reject) =>
			fs.mkdir(uploadsPath, { recursive: true }, (err) => {
				if (err) { reject(err); return; }
				resolve("Directory created successfully:", uploadsPath);
			}),
		);

		for (const key of Object.keys(body)) {
			const rawFilename = body[key].filename;
			let fileName;
			try {
				fileName = sanitizeFilename(rawFilename);
			} catch (e) {
				throw new Error(`File '${rawFilename}' rejected: ${e.message}`);
			}

			const buffer = Buffer.from(body[key].data);
			if (buffer.byteLength > UPLOAD_MAX_BYTES) {
				throw new Error(
					`File '${fileName}' exceeds maximum allowed size of ${UPLOAD_MAX_BYTES} bytes`,
				);
			}

			const filePath = join(uploadsPath, fileName);

			const saveFile = new Promise((resolve, reject) =>
				fs.writeFile(filePath, buffer, "binary", (err) => {
					if (err) { reject(err); return; }
					resolve(fileName);
				}),
			);

			promises.push(saveFile);
		}

		const result = await Promise.all(promises);
		return result;
	}

	async getUploads() {
		const result = await new Promise((resolve, reject) => {
			fs.readdir(uploadsPath, (err, files) => {
				if (err) { reject(err); return; }
				const result = files.filter(f => FILE_EXTENSIONS.some(ex => f.endsWith(ex)));
				resolve(result);
			});
		});
		return result;
	}

	async getApps() {
		const result = await new Promise((resolve, reject) => {
			fs.readdir(appsPath, (err, files) => {
				if (err) { reject(err); return; }
				const result = files.filter(f => FILE_EXTENSIONS.some(ex => f.endsWith(ex)));
				resolve(result);
			});
		});
		return result;
	}
}

export const service = new FileService();
