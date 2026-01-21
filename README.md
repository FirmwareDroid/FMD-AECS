# FMD Android Emulator Connector Service (FMD-AECS)

FMD-AECS is a comprehensive toolkit for building, deploying, and managing Android emulators in Docker containers. This repository provides the infrastructure to run multiple Android emulators with gRPC API exposure via an Envoy reverse proxy, WebRTC support through a Coturn server, and advanced AOSP (Android Open Source Project) build injection capabilities.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Usage](#usage)
  - [Building Docker Emulator Images](#building-docker-emulator-images)
  - [Creating Docker Startup Scripts](#creating-docker-startup-scripts)
  - [AOSP Build Injection](#aosp-build-injection)
- [Configuration](#configuration)
- [Directory Structure](#directory-structure)
- [Contributing](#contributing)
- [License](#license)

## Overview

FMD-AECS is designed to facilitate Android firmware analysis and emulation at scale. It provides tools for:

1. **Emulator Management**: Deploy multiple Android emulators in Docker containers
2. **AOSP Build Injection**: Download and inject firmware components into AOSP builds
3. **Network Configuration**: Expose emulator APIs through Envoy reverse proxy
4. **WebRTC Support**: Enable real-time communication features with Coturn
5. **Firmware Analysis**: Integration with FirmwareDroid backend for firmware analysis

In addition, the repository contains a dedicated ADB streaming service that exposes Android device video, audio and control channels over WebSockets (WSS) for web clients. This ADB streaming service integrates with the emulator fleet and the Envoy proxy to provide authenticated, real-time device streams and remote input injection. See `adb_streaming_service/source/README.md` for detailed usage and configuration.

## Architecture

The system consists of several key components. The high-level flow is:

- Clients (web UI, automation) connect to either Envoy (for gRPC/API) or directly to the ADB Streaming Service (WSS) for real-time streams.
- Envoy routes gRPC/API calls to emulator instances and other backend components.
- The ADB Streaming Service connects to Android devices (local or remote emulators) via ADB and exposes video/audio/control channels over WebSockets.

Here's an updated ASCII diagram showing the ADB streaming service as a first-class component:

```
WebRTC Streaming & gRPC API
┌─────────────────────────────────────────────────────────┐
│                    Client Applications                   │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│              Envoy Reverse Proxy                         │
│         (gRPC API Gateway + Load Balancer)              │
└────────────────────┬────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        │            │            │
┌───────▼──────┐ ┌──▼────────┐ ┌─▼───────────┐
│  Emulator 1  │ │Emulator 2 │ │ Emulator N  │
│  (Docker)    │ │(Docker)   │ │  (Docker)   │
└──────────────┘ └───────────┘ └─────────────┘
        │
┌───────▼──────────────────────────────────────┐
│         Coturn Server (WebRTC)               │
└──────────────────────────────────────────────┘

ADB Streaming Service
┌────────────────────────────────────────────────────────────────────┐
│                            Client Applications                     │
│                  (Web UI, automation, test clients)               │
└──────────────┬─────────────────────────────────────────────────────┘
               │
               │
    ┌──────────▼───────────┐
    │  ADB Streaming Svc    │  <-- WebSockets (WSS) -->  Clients (real-time)
    │  (WebSocket / scrcpy) │
    └──────────┬───────────┘
               │
    ┌──────────┴───────────┐
    │    Emulators /       │
    │    Android Devices   │
    │  (Docker containers) │
    └──────────────────────┘
```

### Key Components:

- **Envoy Proxy**: Routes and load-balances gRPC/HTTP requests to emulator instances and management backends
- **ADB Streaming Service (WebSocket)**: Provides WSS-based streaming of video/audio and control channels for devices; it connects to Android devices via ADB and normalizes control messages (touch/key/scroll) from web clients
- **Android Emulators**: Run in Docker containers with full Android system images (the runtime target for streaming and AOSP builds)
- **Coturn Server**: Provides STUN/TURN services for WebRTC connections used by certain streaming options
- **AOSP Build Tools**: Scripts and templates for building and customizing Android system images and injecting firmware/APEX
- **FirmwareDroid Integration**: Backend connection for firmware download and analysis

## Features

- **Multi-Architecture Support**: Both x86_64 and ARM64 emulator support
- **Scalable Deployment**: Run multiple emulator instances in parallel
- **gRPC API Exposure**: Access emulator APIs through standardized gRPC interface
- **WebRTC Streaming**: Real-time audio/video streaming from emulators
- **AOSP Build Pipeline**: Complete toolchain for building custom Android images
- **Firmware Injection**: Inject firmware packages and APEXs into AOSP builds
- **Docker-based**: Easy deployment and isolation using containers
- **Dynamic Configuration**: Template-based configuration for flexible setups

## Prerequisites

### System Requirements

- **Operating System**: Linux (Ubuntu 20.04+ recommended)
- **CPU**: x86_64 or ARM64 architecture
- **RAM**: Minimum 16GB (32GB+ recommended for multiple emulators)
- **Disk Space**: 100GB+ available (AOSP builds require significant storage)
- **Docker**: Version 20.10+
- **Docker Compose**: Version 2.0+

### Software Dependencies

- **Python**: 3.8 or higher
- **Git**: For repository management
- **AOSP Build Tools** (optional, for building custom images):
  - Java Development Kit (JDK) 11
  - Android SDK Platform Tools
  - Required build dependencies (see AOSP documentation)

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/FirmwareDroid/FMD-AECS.git
cd FMD-AECS
```

### 2. Install Python Dependencies

```bash
pip install -r requirements.txt
```

The required packages include:
- Jinja2 (template engine)
- requests (HTTP library)
- docker (Docker Python SDK)
- werkzeug (utilities)
- tqdm (progress bars)
- filelock (file locking)
- protobuf (Protocol Buffers)

### 3. Set Up Docker

Ensure Docker and Docker Compose are installed and running:

```bash
docker --version
docker-compose --version
```

### 4. Configure Environment

Review and update the environment configuration file with your settings:

```bash
# Review the existing configuration
cat env/.env

# The file contains settings for WebRTC/TURN server
# Edit if you need to customize TURN credentials and server URL
nano env/.env
```

**Note**: The `docker-compose.yaml` and `env/envoy/envoy.yaml` files are auto-generated from templates by the startup scripts. The `env/.env` file is manually configured and primarily contains TURN server credentials for WebRTC functionality.

## Quick Start

### Running Pre-built Emulators

1. **Create startup scripts** for your desired architecture:

```bash
# For ARM64 architecture
python create_docker_startup_scripts.py -c linux/arm64

# For x86_64 architecture
python create_docker_startup_scripts.py -c linux/amd64
```

2. **Start the services**:

```bash
docker-compose up -d
```

3. **Access emulators**:
   - gRPC API: `localhost:8554` (default)
   - ADB: `localhost:5555` (default)
   - SSH: `localhost:2222` (default)

### Building Custom Emulator Images

1. **Download or prepare emulator images**:

```bash
# Build from local images
python create_docker_emulator_images.py -l -i ./emulator_images

# Or download from repository
python create_docker_emulator_images.py \
  -r https://your-repo-url/emulator-images \
  -u username \
  -d docker-registry-url
```

2. **Build Docker images**:

The script will automatically build Docker images for your architecture.

## Usage

### Building Docker Emulator Images

The `create_docker_emulator_images.py` script handles downloading and building emulator Docker images.

**Basic Usage:**

```bash
# Build from local files
python create_docker_emulator_images.py -l -i ./emulator_images

# Download and build from repository
python create_docker_emulator_images.py \
  -r https://repository-url/service/rest/v1/assets?repository=emulator-images \
  -u repository-username \
  -d docker-registry-url
```

**Options:**
- `-l, --create_local`: Build from local files (skip download)
- `-r, --repository-url`: URL to the repository for downloading images
- `-d, --docker-repo-url`: Docker registry URL for pushing images
- `-u, --repository-username`: Authentication username
- `-i, --input-dir`: Directory containing emulator images (default: `./emulator_images`)
- `--file-list`: Comma-separated list of specific files to download

### Creating Docker Startup Scripts

The `create_docker_startup_scripts.py` script generates `docker-compose.yaml` and Envoy configuration files.

**Basic Usage:**

```bash
# Default configuration (ARM64)
python create_docker_startup_scripts.py

# Custom port configuration
python create_docker_startup_scripts.py \
  -g 8554 \
  -a 5555 \
  -s 2222 \
  -c linux/arm64
```

**Options:**
- `-g, --grpc-start-port`: Starting port for gRPC service (default: 8554)
- `-a, --adb-start-port`: Starting port for ADB service (default: 5555)
- `-s, --ssh-start-port`: Starting port for SSH service (default: 2222)
- `-c, --cpu-arch`: CPU architecture - `linux/amd64` or `linux/arm64` (default: linux/arm64)
- `-d, --debug`: Enable debug mode

**Output Files:**
- `docker-compose.yaml`: Docker Compose configuration
- `env/envoy/envoy.yaml`: Envoy proxy configuration

### AOSP Build Injection

The `aosp_build_injector.py` script downloads firmware build files from FirmwareDroid and injects them into AOSP builds.

**Basic Usage:**

```bash
python aosp_build_injector.py \
  -f https://firmwaredroid.example.com \
  -u fmd-username \
  -d docker-repo-username \
  -s /path/to/aosp/source \
  -r docker-registry-url
```

**Options:**
- `-f, --fmd-url`: FirmwareDroid instance URL (required)
- `-u, --fmd-username`: FirmwareDroid username (required)
- `-d, --docker-repo-username`: Docker registry username (required)
- `-s, --aosp-path`: Path to AOSP source root (default: `/home/ubuntu/aosp/aosp12/`)
- `-r, --docker-repo-url`: Docker registry URL for pushing images

**What it does:**
1. Authenticates with FirmwareDroid backend
2. Downloads firmware build files and packages
3. Injects files into AOSP build structure
4. Handles APEX repackaging and signing
5. Builds custom Android system images
6. Uploads resulting images to Docker registry

**Supported AOSP Versions:**
- Android 11
- Android 12
- Android 13

**Supported Lunch Targets:**
- `sdk_phone_arm64-userdebug` (Android 12)
- `sdk_phone64_arm64-userdebug` (Android 13)
- `sdk_phone64_arm64-ap2a-userdebug`

## Configuration

### Environment Variables

Key environment variables can be set in `env/.env`:

```bash
# FirmwareDroid settings
FMD_URL=https://firmwaredroid.example.com
FMD_USERNAME=your-username

# Docker registry settings
DOCKER_REGISTRY_URL=registry.example.com
DOCKER_USERNAME=your-docker-username

# Debug mode
FMD_DEBUG=False
```

### Configuration Files

- **`config.py`**: Main configuration file with build settings, paths, and constants
- **`env/envoy/envoy.yaml`**: Envoy proxy configuration
- **`env/coturn/turnserver.conf`**: Coturn server configuration
- **`templates/docker-compose.yaml`**: Docker Compose template
- **`device_configs/`**: Device-specific AOSP configurations

### Customizing Emulator Configuration

Modify the AVD (Android Virtual Device) configuration in `emulator/avd/` or create new configurations as needed.

## Pi-hole and mitmproxy (Network) configuration

If you run Pi-hole and/or mitmproxy alongside the emulator fleet you may want to redirect emulator HTTP(S) traffic through a local mitmproxy instance for inspection. Below are recommended host-level network and routing rules to mark and route traffic coming from the emulators' Docker network to the host's mitmproxy container. Use these commands on the Docker host (adjust the CIDR, bridge name and gateway to your environment).

Important notes:
- These commands modify the host's packet-marking, routing and kernel settings. Test carefully and document the changes on your host.
- Replace `172.31.250.128/25`, `172.31.250.4` and `br-82bdd2902f50` with the CIDR, gateway and bridge for your Docker network.
- The commands below assume you want to use policy routing: traffic from emulator containers will be marked (fwmark=1) and routed via a specific routing table that forwards to the mitmproxy host/gateway.

1) Disable Docker's iptables behavior (optional but recommended when using manual host-level iptables rules)

Create or edit `/etc/docker/daemon.json` and add:

```json
{
  "iptables": false
}
```

Then restart Docker:

```bash
sudo systemctl restart docker
```

2) Add iptables mangle rules to mark emulator traffic destined for HTTP(S)

Run these on the Docker host (replace the CIDR and bridge/gateway values):

```bash
# According to https://docs.mitmproxy.org/stable/howto/transparent/
sudo sysctl -w net.ipv4.ip_forward=1
sudo sysctl -w net.ipv6.conf.all.forwarding=1

### Setting up TPROXY redirection ###
# mark outgoing HTTP traffic from emulator subnet
sudo iptables -t mangle -A PREROUTING -s 172.31.250.128/25 -p tcp --dport 80 -j MARK --set-mark 1
# mark outgoing HTTPS traffic from emulator subnet
sudo iptables -t mangle -A PREROUTING -s 172.31.250.128/25 -p tcp --dport 443 -j MARK --set-mark 1
```

3) Create a policy routing rule and a routing table that sends marked traffic to the mitmproxy/gateway

```bash
# add rule to use table 100 for marked packets (fwmark == 1)
sudo ip rule add fwmark 1 table 100

# route marked packets via the gateway reachable on the Docker bridge
# replace 172.31.250.4 and br-82bdd2902f50 with your gateway and bridge
sudo ip route add default via 172.31.250.4 dev br-82bdd2902f50 table 100
```

4) Adjust reverse path filtering for the mitmproxy container's network namespace

If mitmproxy runs in a Docker container, reverse-path filtering (rp_filter) inside that container may drop the forwarded packets. To disable rp_filter for the mitmproxy container's network namespace run:

```bash
# get PID of the mitmproxy container and use nsenter to change rp_filter sysctls
MITM_PID=$(docker inspect -f '{{.State.Pid}}' mitmproxy)

sudo nsenter -t $MITM_PID -n sysctl -w net.ipv4.conf.all.rp_filter=0
sudo nsenter -t $MITM_PID -n sysctl -w net.ipv4.conf.default.rp_filter=0
sudo nsenter -t $MITM_PID -n sysctl -w net.ipv4.conf.eth0.rp_filter=0
```

5) TLS interception: trust the mitmproxy CA inside containers

If mitmproxy performs TLS interception you must import the mitmproxy CA into the system trust stores of the containers that need to trust intercepted TLS. An example step (Debian-based images) was already added to the emulator Dockerfile; the typical steps are:

````markdown

## Directory Structure

```
FMD-AECS/
├── aosp_apex_injector.py        # APEX file repackaging and injection
├── aosp_build_injector.py       # Main AOSP build injection script
├── aosp_module_type.py          # AOSP module type definitions
├── aosp_post_build_injector.py  # Post-build file injection
├── aosp_post_build_app_injector.py  # App-specific post-build injection
├── common.py                    # Shared utility functions
├── compare_folders.py           # Folder comparison utilities
├── config.py                    # Main configuration file
├── ConfigManager.py             # Configuration management
├── create_docker_emulator_images.py  # Build emulator Docker images
├── create_docker_startup_scripts.py  # Generate Docker Compose configs
├── fmd_backend_requests.py      # FirmwareDroid API client
├── parse_lddtree_to_json.py     # Dependency tree parser
├── setup_logger.py              # Logging configuration
├── shell_command.py             # Shell command utilities
├── requirements.txt             # Python dependencies
│
├── device_configs/              # Device-specific AOSP configurations
│   ├── development/             # Development device configs
│   └── native_injection/        # Native library injection configs
│
├── emulator/                    # Emulator Docker configurations
│   ├── Dockerfile_arm64         # ARM64 emulator Dockerfile
│   ├── Dockerfile_x86_64        # x86_64 emulator Dockerfile
│   ├── Dockerfile_base_emulator_* # Base emulator Dockerfiles
│   ├── emulator_start.sh        # Emulator startup script
│   ├── avd/                     # Android Virtual Device configs
│   └── prebuilts/               # Prebuilt binaries (ignored)
│
├── env/                         # Environment configurations
│   ├── .env                     # Environment variables
│   ├── envoy/                   # Envoy proxy configuration
│   ├── coturn/                  # Coturn server configuration
│   └── nginx/                   # Nginx web server (for ACME/Let's Encrypt)
│
├── templates/                   # Configuration templates
│   ├── docker-compose.yaml      # Docker Compose template
│   ├── envoy.yaml               # Envoy template
│   ├── docker_emulator.txt      # Emulator service template
│   ├── envoy_match.txt          # Envoy route matching template
│   ├── envoy_cluster.txt        # Envoy cluster template
│   ├── build_image.py           # AOSP image building script
│   ├── file_contexts            # SELinux file contexts
│   └── apex/                    # APEX configuration templates
│
├── image_artefacts/             # Built image artifacts (ignored, created during build)
│   ├── arm64-v8a/               # ARM64 emulator image files (auto-generated)
│   └── x86_64/                  # x86_64 emulator image files (auto-generated)
├── out/                         # Build output directory (ignored)
├── nexus/                       # Nexus repository integration
└── testing_service/             # Testing utilities and services
```

## Contributing

Contributions are welcome! Please follow these guidelines:

1. **Fork the repository** and create a feature branch
2. **Follow existing code style** and conventions
3. **Test your changes** thoroughly
4. **Document new features** in the README
5. **Submit a pull request** with a clear description

### Development Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Linux/Mac
# or
venv\Scripts\activate  # On Windows

# Install dependencies
pip install -r requirements.txt
```

## License

Please refer to the repository for license information.

## Support and Contact

For issues, questions, or contributions:
- **GitHub Issues**: https://github.com/FirmwareDroid/FMD-AECS/issues
- **FirmwareDroid Project**: https://github.com/FirmwareDroid

## Acknowledgments

This project is part of the FirmwareDroid ecosystem for Android firmware analysis and security research.

