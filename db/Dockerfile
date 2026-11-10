# Imagen de la BD: PostgreSQL 15.2 + TimescaleDB 2.10.2 + PostGIS 3.3.2 + pgRouting 4.0.1.
#
# La base está fijada por digest: es la `pg15-latest` con la que se crearon los datos
# (2023). Hoy `pg15-latest` apunta a versiones mucho más nuevas de TimescaleDB, que
# no cargan una BD con la extensión 2.10.2 sin un ALTER EXTENSION ... UPDATE planeado.
# pgRouting también fijado: una versión distinta del .so exigiría actualizar la extensión.
# Actualizar cualquiera de las dos es una migración propia (respaldo + prueba), no un rebuild (D17).
FROM timescale/timescaledb-ha@sha256:a8e3322e1cf936828698cb4de2a9c4b59acae1b123909f023bb15f42270af95d

USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends postgresql-15-pgrouting=4.0.1-1.pgdg22.04+1 && \
    rm -rf /var/lib/apt/lists/*
USER postgres
