FROM python:3.12-slim
ARG BUILD_VERSION=0.4.0-dev57
ARG BUILD_ARCH=amd64
LABEL io.hass.type="app" io.hass.version="${BUILD_VERSION}" io.hass.arch="${BUILD_ARCH}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8101 PYTHONTZPATH=""
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8101
CMD ["python","app.py"]
