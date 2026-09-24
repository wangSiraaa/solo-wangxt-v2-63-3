FROM python:3.11-slim

# GDAL/GEOS/PROJ（Django GIS 后端需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

ENV GDAL_LIBRARY_PATH=/usr/lib/aarch64-linux-gnu/libgdal.so

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py runserver 0.0.0.0:8000"]
