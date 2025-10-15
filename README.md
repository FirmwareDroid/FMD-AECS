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

## Architecture

The system consists of several key components:

```
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
```

### Key Components:

- **Envoy Proxy**: Routes and load-balances gRPC requests to emulator instances
- **Android Emulators**: Run in Docker containers with full Android system images
- **Coturn Server**: Provides STUN/TURN services for WebRTC connections
- **AOSP Build Tools**: Scripts for building and customizing Android system images
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

Copy and configure the environment files:

```bash
# Environment variables for services
cp env/.env.example env/.env
# Edit env/.env with your configuration
```

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
│   └── nginx/                   # Nginx configuration (optional)
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
├── image_artefacts/             # Built image artifacts (ignored)
├── out/                         # Build output directory (ignored)
├── nexus/                       # Nexus repository integration
└── testing_service/             # Testing utilities
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

# Run tests (if available)
# pytest tests/
```

## License

Please refer to the repository for license information.

## Support and Contact

For issues, questions, or contributions:
- **GitHub Issues**: https://github.com/FirmwareDroid/FMD-AECS/issues
- **FirmwareDroid Project**: https://github.com/FirmwareDroid

## Acknowledgments

This project is part of the FirmwareDroid ecosystem for Android firmware analysis and security research.










