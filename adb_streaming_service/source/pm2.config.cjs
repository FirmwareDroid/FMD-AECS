const { join } = require("path");

// Load .env only in development / test — not in production where env vars are
// injected by the container runtime.
if (!process.env.NODE_ENV || ["development", "test"].includes(process.env.NODE_ENV)) {
	try { require("dotenv").config({ path: join(__dirname, ".env") }); } catch (e) { /* dotenv optional */ }
}

module.exports = {
	apps: [
		{
			name: "adb-streamer",
			script: "index.js",
			watch: false,
			// Single instance: the service holds per-device ADB state in memory.
			// Multi-process cluster mode would shard that state across workers,
			// causing requests to fail when routed to the wrong worker.
			instances: 1,
			autorestart: true,
			exec_mode: "fork",
			env: {
				HOST: "0.0.0.0",
				PORT: "9001",
			},
		},
	],
};
