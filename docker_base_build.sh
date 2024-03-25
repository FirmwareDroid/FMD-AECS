#!/bin/bash
# Build the images
docker build -t fmd-emulator_arm64 --platform linux/arm64 -f ./emulator/Dockerfile_base_emulator_arm64 .
docker build -t fmd-emulator_x86_64 -f ./emulator/Dockerfile_base_emulator_x86_64 .