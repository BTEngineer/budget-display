FROM ghcr.io/home-assistant/base:3.24

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache python3 py3-pip py3-setuptools py3-wheel

WORKDIR /opt/budget-display
COPY pyproject.toml ./
COPY requirements.lock ./
COPY src ./src
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        --require-hashes -r requirements.lock \
    && python3 -m pip install --no-cache-dir --break-system-packages \
        --no-build-isolation --no-deps .

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
