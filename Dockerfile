FROM python:3.12-slim

WORKDIR /app

COPY python/ ./python/
RUN pip install --no-cache-dir ./python[all]

COPY typescript/ ./typescript/
COPY java/ ./java/

ENTRYPOINT ["python", "-c", "from unified_channel import ChannelManager; print('unified-channel ready')"]
