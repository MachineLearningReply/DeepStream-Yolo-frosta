# Runtime environment for the unchanged production pipeline (deepstream_pipeline/main.py) in Docker.
# Only dependencies live in the image; the repo is mounted at /home/reply/DeepStream-Yolo-frosta (see compose.yaml),
# so the code's hard-coded paths, the models/engines and the YOLO parser library are used exactly as on the host.
# Based on the frosta-edge Dockerfile. The base image must match the host's JetPack major version (JetPack 6 / L4T R36).
ARG DEEPSTREAM_IMAGE=nvcr.io/nvidia/deepstream:7.1-triton-multiarch
FROM ${DEEPSTREAM_IMAGE}

ARG ARAVIS_VERSION=0.8.34
ARG TISCAMERA_VERSION=1.1.2
ARG TISCAMERA_DEB=tiscamera_1.1.2.4191_arm64_ubuntu_2204.deb
ARG PYDS_VERSION=1.2.0
ARG PYDS_WHEEL=pyds-1.2.0-cp310-cp310-linux_aarch64.whl

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils \
        python3-pip python3-gi python3-gst-1.0 \
        meson ninja-build pkg-config gettext zlib1g-dev \
        libxml2-dev libglib2.0-dev libusb-1.0-0-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
        gobject-introspection libgirepository1.0-dev \
    && rm -rf /var/lib/apt/lists/*

# aravis: GStreamer source for GigE Vision cameras (aravissrc), installed to /usr/local like on the host
RUN curl -fsSL "https://github.com/AravisProject/aravis/releases/download/${ARAVIS_VERSION}/aravis-${ARAVIS_VERSION}.tar.xz" \
        | tar xJ -C /tmp \
    && cd /tmp/aravis-${ARAVIS_VERSION} \
    && meson setup build --prefix=/usr/local -Dgst-plugin=enabled -Dusb=enabled \
        -Dviewer=disabled -Dintrospection=enabled -Ddocumentation=disabled -Dtests=false \
    && ninja -C build install \
    && ldconfig \
    && rm -rf /tmp/aravis-${ARAVIS_VERSION}

# tiscamera: debayering element tcamconvert
RUN curl -fsSL -o /tmp/${TISCAMERA_DEB} \
        "https://github.com/TheImagingSource/tiscamera/releases/download/v-tiscamera-${TISCAMERA_VERSION}/${TISCAMERA_DEB}" \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/${TISCAMERA_DEB} \
    && rm -rf /var/lib/apt/lists/* /tmp/${TISCAMERA_DEB}

# The pipeline loads the message payload library from the sources tree (pipeline_builder.py).
# If the image doesn't ship it there, point that path to NVIDIA's prebuilt copy.
RUN MSG=/opt/nvidia/deepstream/deepstream/sources/libs/nvmsgconv/libnvds_msgconv.so \
    && if [ ! -e "$MSG" ]; then \
         mkdir -p "$(dirname "$MSG")" && ln -s /opt/nvidia/deepstream/deepstream/lib/libnvds_msgconv.so "$MSG"; \
       fi \
    && ls -la "$MSG"

# Python packages used by deepstream_pipeline/. numpy < 2: pyds is built against numpy 1.x
RUN curl -fsSL -o /tmp/${PYDS_WHEEL} \
        "https://github.com/NVIDIA-AI-IOT/deepstream_python_apps/releases/download/v${PYDS_VERSION}/${PYDS_WHEEL}" \
    && python3 -m pip install --no-cache-dir /tmp/${PYDS_WHEEL} "numpy<2" opencv-python-headless==4.10.0.84 \
        redis==6.1.1 psutil \
    && rm /tmp/${PYDS_WHEEL}

ENTRYPOINT ["python3", "-u"]
