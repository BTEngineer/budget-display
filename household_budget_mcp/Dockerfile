FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache python3 py3-pip

WORKDIR /opt/budget-display
COPY pyproject.toml ./
COPY requirements.lock ./
COPY src ./src
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.lock \
    && python3 -m pip install --no-cache-dir --break-system-packages --no-deps .

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
