#!/usr/bin/env bash
#
# 在无 root、无系统 PostgreSQL/GDAL 的机器上一键起本地环境：
# 使用 micromamba（用户态）安装 PostgreSQL+PostGIS+GDAL，pip 装 Django 栈。
#
set -euo pipefail
cd "$(dirname "$0")"

MAMBA="${MAMBA_BIN:-/tmp/bin/micromamba}"
ENV_PREFIX="$(pwd)/.condaenv"
PGDATA="$(pwd)/.pgdata"
SOCKDIR=/tmp/pgsock
export MAMBA_ROOT_PREFIX="$(pwd)/.micromamba" MAMBA_NO_BANNER=1

if [ ! -x "$MAMBA" ]; then
  curl -Ls https://micro.mamba.pm/api/micromamba/linux-aarch64/latest | tar -xj -C /tmp bin/micromamba
fi

if [ ! -d "$ENV_PREFIX" ]; then
  "$MAMBA" create -y -p "$ENV_PREFIX" -c conda-forge \
    "python=3.11" gdal libspatialite "postgresql>=15" postgis psycopg2
  "$MAMBA" run -p "$ENV_PREFIX" python -m pip install -r requirements.txt
fi

mkdir -p "$SOCKDIR"
if [ ! -d "$PGDATA" ]; then
  "$MAMBA" run -p "$ENV_PREFIX" initdb -D "$PGDATA" -U postgres --auth=trust --no-locale --encoding=UTF8
fi

if ! "$MAMBA" run -p "$ENV_PREFIX" pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  "$MAMBA" run -p "$ENV_PREFIX" pg_ctl -D "$PGDATA" -l "$PGDATA/logfile" \
    -o "-k $SOCKDIR -p 5433 -c listen_addresses=''" start
  sleep 2
  "$MAMBA" run -p "$ENV_PREFIX" psql -h "$SOCKDIR" -p 5433 -U postgres -tc \
    "SELECT 1 FROM pg_database WHERE datname='sanitation'" | grep -q 1 \
    || "$MAMBA" run -p "$ENV_PREFIX" createdb -h "$SOCKDIR" -p 5433 -U postgres sanitation
fi

export PATH="$ENV_PREFIX/bin:$PATH" LD_LIBRARY_PATH="$ENV_PREFIX/lib"
python manage.py migrate --noinput
python manage.py generate_mock_images
exec python manage.py runserver 0.0.0.0:8000
