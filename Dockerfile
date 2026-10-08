# PALBE — Fase 4b del plan de integracion en la Plataforma SGE.
#
# Base: python:3.14-slim, la MISMA version que corre hoy en produccion
# (.venv/pyvenv.cfg: 3.14.4). Elegir otra version no aporta nada y anade
# una variable: aunque uv.lock resuelve las mismas versiones de paquete
# en 3.13 y 3.14, los wheels son distintos (cp313 vs cp314, ABI
# incompatibles), es decir, binarios compilados distintos de los que
# generan hoy los modelos de produccion.
FROM python:3.14-slim

# libgomp1 es el UNICO paquete de sistema necesario -- medido, no
# supuesto (ver el Anexo del documento principal): sin el, "import
# lightgbm" muere con OSError: libgomp.so.1. matplotlib trae sus propias
# fuentes DejaVu (fonts-dejavu-core sobra) y pip/httpx/anthropic usan el
# bundle de certifi, no el almacen del sistema (ca-certificates sobra).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# uv, vendored desde su propia imagen oficial -- sin pip intermedio.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copiar primero solo los manifiestos: cachea la capa de dependencias
# para que un cambio de codigo no fuerce un uv sync completo de nuevo.
COPY pyproject.toml uv.lock .python-version ./

# --frozen: nunca resuelve por su cuenta, solo instala EXACTAMENTE lo
# que dice uv.lock. Si el lockfile y el pyproject no coinciden, falla
# aqui, en el build, no en produccion con dependencias inesperadas.
# --no-dev: pytest y compania no entran en la imagen (Paso 5, checkpoint
# de "uv sync --frozen --no-dev" ya verificado sin ninguna linea
# "Building..." -- no hace falta toolchain de compilacion).
RUN uv sync --frozen --no-dev --no-install-project

# Codigo de la aplicacion (.dockerignore ya excluye .venv/, outputs/,
# user_data/, palbe_backups/, *.db*, .env, tests/, docs/).
COPY . .

# Usuario no-root. Los directorios de datos se crean como root (antes
# de bajar privilegios) porque en el primer arranque contra un volumen
# nuevo la app los crea igualmente (OUTPUTS_DIR.mkdir(), etc.) -- esto
# solo evita un error de permisos si el volumen se monta vacio y root-owned.
RUN groupadd -r palbe && useradd -r -g palbe -d /app palbe \
    && mkdir -p /data/db /data/outputs /data/user_data /data/backups \
    && chown -R palbe:palbe /app /data
USER palbe

ENV PATH="/app/.venv/bin:$PATH" \
    PALBE_CONTAINER=1 \
    PALBE_DB_PATH=/data/db/palbe.db \
    PALBE_OUTPUTS_DIR=/data/outputs \
    PALBE_DATA_ROOT=/data/user_data \
    PALBE_BACKUP_DIR=/data/backups

EXPOSE 8000

# /health esta en la lista blanca del middleware (Fase 4a): responde sin
# sesion. Antes de ese cambio, un HEALTHCHECK habria recibido un 303 a
# /login y el contenedor nunca se habria marcado healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

# Sin --workers: el estado en memoria documentado en la Fase 0
# (_USER_LAST_SEEN, _LOGIN_RATE, el ContextVar _CURRENT_USER_ID) ata la
# app a un solo proceso. Multi-worker es la Fase 9, junto con Postgres.
CMD ["uvicorn", "app_palbe_4:app", "--host", "0.0.0.0", "--port", "8000"]
