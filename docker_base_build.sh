#!/bin/bash

ARCH=$(uname -m)

if [[ "$ARCH" == "x86_64" ]]; then
    docker buildx build --load -t fmd-emulator_x86_64 --platform linux/amd64 -f ./emulator/Dockerfile_base_emulator_x86_64 .
elif [[ "$ARCH" == "aarch64" ]]; then
    docker buildx build --load -t fmd-emulator_arm64 --platform linux/arm64 -f ./emulator/Dockerfile_base_emulator_arm64 .
else
    echo "Unsupported architecture: $ARCH"
    exit 1
fi