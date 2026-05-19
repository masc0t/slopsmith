# ── Stage 1: Build RsCli ─────────────────────────────────────────────────
# TARGETARCH matches the final image (arm64 on Apple Silicon, amd64 on Intel/x86 servers).
# RsCli must match that arch: linux-x64 binaries do not run on linux/arm64.
FROM python:3.12-slim AS builder
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*

RUN curl -sL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
    && chmod +x /tmp/dotnet-install.sh \
    && /tmp/dotnet-install.sh --channel 10.0 --install-dir /usr/share/dotnet \
    && ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet

ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1

RUN git clone --depth 1 https://github.com/iminashi/Rocksmith2014.NET.git /tmp/rs2014

COPY rscli/RsCli.fsproj /tmp/rs2014/tools/RsCli/
COPY rscli/Program.fs /tmp/rs2014/tools/RsCli/

RUN sed -i 's|</PropertyGroup>|<NuGetAudit>false</NuGetAudit></PropertyGroup>|' /tmp/rs2014/Directory.Build.props \
    && cd /tmp/rs2014/tools/RsCli \
    && arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && case "$arch" in \
         arm64|aarch64) RID=linux-arm64 ;; \
         amd64|x86_64) RID=linux-x64 ;; \
         *) echo "Unsupported build architecture: $arch" >&2; exit 1 ;; \
       esac \
    && dotnet publish -c Release -r "$RID" --self-contained -o /opt/rscli

# ── Stage 1b: Build native vgmstream-cli for target arch ─────────────────────
FROM python:3.12-slim AS vgmstream-builder
ARG VGMSTREAM_REF=r2083

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    pkg-config \
    yasm \
    libmpg123-dev \
    libvorbis-dev \
    libspeex-dev \
    libopus-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${VGMSTREAM_REF}" https://github.com/vgmstream/vgmstream.git /tmp/vgmstream

RUN cmake -S /tmp/vgmstream -B /tmp/vgmstream/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_V123=OFF \
        -DBUILD_AUDACIOUS=OFF \
        -DBUILD_SHARED_LIBS=OFF \
        -DUSE_FFMPEG=OFF \
    && cmake --build /tmp/vgmstream/build --config Release --target vgmstream_cli -j"$(nproc)" \
    && mkdir -p /out \
    && cp /tmp/vgmstream/build/cli/vgmstream-cli /out/vgmstream-cli

# ── Stage 1c: Fetch static ffmpeg ─────────────────────────────────────────
# Throwaway stage — only the ffmpeg/ffprobe binaries cross into the final
# image via COPY. Doing the download here (rather than in stage 2) means
# the final image never has to install curl, which transitively pulls in
# libcurl4t64 → librtmp1 → libgnutls30t64 (and therefore gnutls28 with
# its unfixed HIGH CVEs). Alpine is used because it's tiny and the
# download tools don't need any of Debian's TLS baggage.
#
# Source: BtbN/FFmpeg-Builds (GPL static build, 7.1 series).
# BtbN publishes dated release tags (autobuild-YYYY-MM-DD-HH-MM) that
# yield immutable URLs — the versioned tarballs never disappear, unlike
# JVS rolling releases. Includes libvorbis (confirmed --enable-libvorbis
# in the configure line), so Sloppak's .ogg output path is unaffected.
#
# To bump: pick a new autobuild-* tag from
#   https://github.com/BtbN/FFmpeg-Builds/releases
# download the two linux gpl-7.1 tarballs, re-run
#   sha256sum ffmpeg-*-linux{64,arm64}-gpl-7.1.tar.xz
# and update FFMPEG_RELEASE + both SHA256 ARGs below.
FROM alpine:3.20 AS ffmpeg-fetcher
ARG TARGETARCH
ARG FFMPEG_RELEASE=autobuild-2026-05-18-18-09
ARG FFMPEG_BUILD_AMD64=ffmpeg-n7.1.4-5-ged860ef7d9-linux64-gpl-7.1.tar.xz
ARG FFMPEG_BUILD_ARM64=ffmpeg-n7.1.4-5-ged860ef7d9-linuxarm64-gpl-7.1.tar.xz
ARG FFMPEG_SHA256_AMD64=f0dc9851561a64a9020e013a9d39ce344a06373444ab301c946bc2d9caecacf5
ARG FFMPEG_SHA256_ARM64=fef25a656a5d5e6c2a860ca45445e73f64958c9a8a5910cd5676ff23b99a65fa
RUN apk add --no-cache curl xz \
    && arch="${TARGETARCH:-$(apk --print-arch)}" \
    && case "$arch" in \
         arm64|aarch64) FFMPEG_TARBALL="${FFMPEG_BUILD_ARM64}"; FFMPEG_SHA256="${FFMPEG_SHA256_ARM64}" ;; \
         amd64|x86_64)  FFMPEG_TARBALL="${FFMPEG_BUILD_AMD64}"; FFMPEG_SHA256="${FFMPEG_SHA256_AMD64}" ;; \
         *) echo "Unsupported arch: $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_RELEASE}/${FFMPEG_TARBALL}" -o /tmp/ffmpeg.tar.xz \
    && echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
    && mkdir -p /tmp/ffmpeg-extract /out \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg-extract --strip-components=1 \
    && cp /tmp/ffmpeg-extract/bin/ffmpeg /tmp/ffmpeg-extract/bin/ffprobe /out/ \
    && cp /tmp/ffmpeg-extract/LICENSE.txt /out/LICENSE.txt \
    && rm -rf /tmp/ffmpeg-extract /tmp/ffmpeg.tar.xz

# ── Stage 2: Final image ────────────────────────────────────────────────
FROM python:3.12-slim
# Re-declare the ffmpeg ARGs so their values are available to LABEL below.
# ARG values don't cross stage boundaries in multi-stage builds; defaults
# must be repeated here to take effect when no --build-arg is supplied.
ARG FFMPEG_RELEASE=autobuild-2026-05-18-18-09
ARG FFMPEG_BUILD_AMD64=ffmpeg-n7.1.4-5-ged860ef7d9-linux64-gpl-7.1.tar.xz
ARG FFMPEG_BUILD_ARM64=ffmpeg-n7.1.4-5-ged860ef7d9-linuxarm64-gpl-7.1.tar.xz

# Apply latest security updates to base packages (clears glibc deb13u3 and
# similar). Done first so any subsequent installs resolve against the
# patched versions rather than the stale ones baked into the base image.
RUN apt-get update \
    && apt-get -y upgrade \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Runtime packages.
#
# NOTE: ffmpeg is intentionally NOT installed via apt. The apt `ffmpeg`
# package drags in the full codec + TLS + graphics dependency tree
# (mbedtls, gnutls28, mesa, x264, tiff, openjpeg2, libcaca, harfbuzz,
# cairo, openldap, libcdio…), almost all of which has unfixed CVEs and
# none of which Slopsmith uses. We pull a static ffmpeg binary further
# down instead.
#
# vgmstream-cli is also built with -DUSE_FFMPEG=OFF (see stage 1b), so
# we don't need the libav* runtime libraries either — Rocksmith's WEM
# files use Wwise Vorbis, which vgmstream decodes natively. Dropping
# libav* also drops their transitive deps on mbedtls and gnutls28.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fluidsynth \
    fluid-soundfont-gm \
    libsndfile1 \
    # Runtime shared libraries for the natively-built vgmstream-cli.
    # `BUILD_SHARED_LIBS=OFF` in the builder stage only static-links
    # vgmstream's own libs; the external codec dependencies it linked
    # against (mpg123, vorbis, speex, opus) are still dynamic and need
    # their runtime packages here.
    libmpg123-0 \
    libvorbisfile3 \
    libspeex1 \
    libopus0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# Static ffmpeg + ffprobe binaries from the throwaway fetcher stage above.
# BtbN GPL builds statically link their codec deps and don't pull in
# GnuTLS/mbedTLS, mesa, x264, cairo, etc. No CVE surface from the system
# codec stack; ~80 MB on disk.
#
# NOTE (GPL): the static ffmpeg binary is licensed under GPL v2+.
# LICENSE.txt from the BtbN tarball is copied into /usr/share/doc/ffmpeg/
# so the license text is present in the runtime image.
#
# If this image is redistributed publicly, the GPL requires that the
# Corresponding Source for this ffmpeg build also be made available.
# BtbN publishes full build configuration and source references at:
#   https://github.com/BtbN/FFmpeg-Builds  (tag: FFMPEG_RELEASE ARG)
# Ensure your redistribution method meets GPL conveyance requirements —
# either by pointing recipients to BtbN's source or by hosting it yourself.
COPY --from=ffmpeg-fetcher /out/ffmpeg /out/ffprobe /usr/local/bin/
COPY --from=ffmpeg-fetcher /out/LICENSE.txt /usr/share/doc/ffmpeg/LICENSE.txt
RUN chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe
# Record provenance so the exact BtbN source can be located for GPL compliance
# or debugging. Inspect with: docker inspect <image> | grep -A5 ffmpeg
LABEL org.slopsmith.ffmpeg.release="${FFMPEG_RELEASE}" \
      org.slopsmith.ffmpeg.source.amd64="${FFMPEG_BUILD_AMD64}" \
      org.slopsmith.ffmpeg.source.arm64="${FFMPEG_BUILD_ARM64}" \
      org.slopsmith.ffmpeg.upstream="https://github.com/BtbN/FFmpeg-Builds"

# Native vgmstream-cli built against the image's own libraries
COPY --from=vgmstream-builder /out/vgmstream-cli /usr/local/bin/vgmstream-cli
RUN chmod +x /usr/local/bin/vgmstream-cli

# Copy RsCli from builder (no .NET SDK in final image)
COPY --from=builder /opt/rscli /opt/rscli

WORKDIR /app

# Upgrade pip itself before installing requirements — clears the pip CVEs
# (CVE-2025-8869, CVE-2026-6357, CVE-2026-1703) that ship with the base.
# Pinned for reproducibility; bump PIP_VERSION when a newer release is needed.
ARG PIP_VERSION=26.1.1
RUN pip install --no-cache-dir "pip==${PIP_VERSION}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY lib/ /app/lib/
COPY static/ /app/static/
COPY plugins/ /app/plugins/
COPY server.py /app/
COPY main.py /app/
COPY VERSION /app/

ENV PYTHONPATH=/app/lib:/app
ENV RSCLI_PATH=/opt/rscli/RsCli
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1

EXPOSE 8000

# main.py calls configure_logging() before uvicorn.run(..., log_config=None),
# which prevents uvicorn from applying its default dictConfig.  This ensures
# the structlog pipeline is active for ALL uvicorn messages — including the
# early lifecycle lines ("Started server process", "Waiting for application
# startup") that fire before the ASGI startup hook.
CMD python main.py
