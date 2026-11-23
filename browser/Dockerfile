FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb \
        xauth \
        x11vnc \
        novnc \
        websockify \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /profile && chown -R pwuser:pwuser /profile

COPY browser/entrypoint.sh /usr/local/bin/browser-entrypoint.sh
COPY browser/cdp_proxy.py /usr/local/bin/cdp-proxy.py
RUN chmod +x /usr/local/bin/browser-entrypoint.sh /usr/local/bin/cdp-proxy.py

ENV DISPLAY=:99 \
    PROFILE_DIR=/profile \
    BROWSER_SCREEN=1440x900x24

USER pwuser
EXPOSE 6080 9222

ENTRYPOINT ["/usr/local/bin/browser-entrypoint.sh"]
