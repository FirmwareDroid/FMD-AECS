#!/bin/bash
docker buildx build --load -t fmd-emulator_arm64 --platform linux/arm64 -f ./emulator/Dockerfile_base_emulator_arm64 .
docker buildx build --load -t fmd-emulator_x86_64 --platform linux/amd64 -f ./emulator/Dockerfile_base_emulator_x86_64 .
