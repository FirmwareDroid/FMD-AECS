const { join } = require("path");
if (
	!!process.env.NODE_ENV ||
	["development", "test"].includes(process.env.NODE_ENV)
) {
	require("dotenv").config({ path: join(__dirname, ".env") });
}

module.exports = {
	apps: [
		{
			name: "adb-streamer",
			script: "index.js",
			watch: false,
			instances: "max",
			autorestart: false,
			exec_mode: "cluster",
			env: {
				HOST: "0.0.0.0",
				PORT: "9001",
			},
		},
	],
};
