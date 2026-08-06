FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

ARG ACVPATCHER_VERSION=1.0.8
ARG ACVPATCHER_URL="https://github.com/pilgun/acvpatcher/releases/download/${ACVPATCHER_VERSION}/ACVPatcher-linux.zip"

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    unzip \
    adb \
    aapt \
    tini \
    && curl -fsSL -o /tmp/ACVPatcher-linux.zip "$ACVPATCHER_URL" \
    && unzip -q /tmp/ACVPatcher-linux.zip -d /tmp/acvpatcher \
    && install -m 0755 /tmp/acvpatcher/ACVPatcher /usr/local/bin/acvpatcher \
    && adb version >/dev/null \
    && aapt version >/dev/null \
    && rm -rf /tmp/ACVPatcher-linux.zip /tmp/acvpatcher \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/acvtool
COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade "pip<25" setuptools wheel && \
    python -m pip install -r requirements.txt && \
    python -m pip install acvtool==2.3.6 && \
    python -c "import json,pathlib,acvtool; s=pathlib.Path(acvtool.__file__).parent/'smiler'; c=s/'config.json'; c.exists() or c.write_text(json.dumps({'ADB':'/usr/bin/adb','AAPT':'/usr/bin/aapt','ACVPATCHER':'/usr/local/bin/acvpatcher'}, indent=2)+'\\n', encoding='utf-8')" && \
    python -c "import acvtool.smiler.acv"

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /work
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["acv", "--help"]






