#!/bin/bash
# Define the local repository
LOCAL_REPO="localhost:5000"

# Build the images using buildx
docker buildx build -t fmd-emulator_arm64 --platform linux/arm64 -f ./emulator/Dockerfile_base_emulator_arm64 .
docker buildx build -t fmd-emulator_x86_64 --platform linux/amd64 -f ./emulator/Dockerfile_base_emulator_x86_64 .

# Tag the images with the local repository
docker tag fmd-emulator_arm64 $LOCAL_REPO/fmd-emulator_arm64:latest
docker tag fmd-emulator_x86_64 $LOCAL_REPO/fmd-emulator_x86_64:latest

# Push the images to the local repository
docker push $LOCAL_REPO/fmd-emulator_arm64:latest
docker push $LOCAL_REPO/fmd-emulator_x86_64:latest