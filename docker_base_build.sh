#!/bin/bash
# Build the images
docker buildx -t fmd-emulator_arm64 --platform linux/arm64 -f ./emulator/Dockerfile_base_emulator_arm64 .
docker buildx -t fmd-emulator_x86_64 -f ./emulator/Dockerfile_base_emulator_x86_64 .
