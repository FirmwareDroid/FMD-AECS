#!/bin/bash

docker build -f ./emulator/Dockerfile_base_emulator_arm64 .
docker build -f ./emulator/Dockerfile_base_emulator_x86_64 .