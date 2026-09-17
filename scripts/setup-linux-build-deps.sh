#!/usr/bin/env bash
#
# Install the system packages needed to build local_video (publisher/subscriber)
# with NVENC AV1 on Ubuntu 24.04 x86_64.
#
# Everything else (rustup + Rust 1.97.1, git submodules, clang 21) is installed
# user-locally and needs no root. This script is the only part that needs sudo.

set -euo pipefail

sudo apt update -y

# Core build + bindgen + yuv-sys jpeg feature (libjpeg.pc).
# Linking libwebrtc.a and its DesktopCapturer needs the X11/DRM/GBM stack.
# libasound2-dev is for the audio device backend.
sudo apt install -y \
  build-essential \
  libc6-dev \
  pkg-config \
  libglib2.0-dev \
  libjpeg-turbo8-dev \
  lld \
  libasound2-dev \
  libssl-dev \
  libx11-dev \
  libgl1-mesa-dev \
  libxext-dev \
  libdrm-dev \
  libgbm-dev \
  libxfixes-dev \
  libxdamage-dev \
  libxrandr-dev \
  libxcomposite-dev \
  libva-dev

# NVENC: webrtc-sys/build.rs compiles the NVIDIA encoders only if
# $CUDA_HOME/include/cuda.h exists. libcuda/libnvcuvid are dlopened at runtime
# from the driver (already present: 595.84), so we need CUDA *headers* only,
# not the full multi-GB toolkit.
sudo apt install -y nvidia-cuda-dev

echo
echo "Done. cuda.h at: $(ls /usr/include/cuda.h 2>/dev/null || echo 'NOT FOUND')"
