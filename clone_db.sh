#!/bin/bash
# Script para clonar la base de datos 'clm_db' de PostgreSQL

SOURCE_DB="clm_db"
TARGET_DB="clm_db_clone"
DB_USER="grivy_clm"

echo "================================================="
echo "Iniciando clonación de $SOURCE_DB a $TARGET_DB..."
echo "================================================="

# 1. Terminar las conexiones activas a la base de datos origen
# PostgreSQL no permite clonar (usar como plantilla) una base de datos si hay usuarios conectados a ella.
echo "[1/3] Cerrando conexiones activas a la base de datos origen..."
sudo -u postgres psql -c "SELECT pg_terminate_backend(pg_stat_activity.pid) FROM pg_stat_activity WHERE pg_stat_activity.datname = '$SOURCE_DB' AND pid <> pg_backend_pid();" > /dev/null

# 2. Crear la nueva base de datos usando la original como plantilla
echo "[2/3] Clonando la base de datos (esto puede tardar un momento dependiendo del tamaño)..."
# Si la base de datos destino ya existe, el script dará un error aquí de forma natural.
sudo -u postgres psql -c "CREATE DATABASE $TARGET_DB WITH TEMPLATE $SOURCE_DB OWNER $DB_USER;"

# 3. Finalizar
if [ $? -eq 0 ]; then
    echo "[3/3] ¡Éxito! La base de datos '$TARGET_DB' ha sido creada correctamente con el propietario '$DB_USER'."
else
    echo "[3/3] Hubo un error al intentar clonar la base de datos. Verifica si '$TARGET_DB' ya existe."
fi
