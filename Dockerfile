FROM python:3.12-slim

# eccodes and rasterio wheels bundle their native libraries (ecCodes, GDAL),
# so no system packages are needed.
WORKDIR /app
COPY pyproject.toml ./
COPY pipeline ./pipeline
COPY web ./web
RUN pip install --no-cache-dir . \
 && useradd --system --uid 10001 app \
 && mkdir /data && chown app /data

USER app
ENV WTP_DATA_DIR=/data WTP_WEB_DIR=/app/web PYTHONUNBUFFERED=1
CMD ["python", "-m", "pipeline.run", "--loop", "--interval", "600"]
