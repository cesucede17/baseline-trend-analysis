"""
Fixtures del arnes de tests (Fase 2b).

Filosofia: cada test recibe una COPIA temporal de datos_prueba/palbe.db
(nunca el fixture compartido), con las 5 contrasenas reales sustituidas por
una conocida SOLO en esa copia. La app se importa una unica vez por sesion
(el import es caro: lightgbm, xgboost, plotly...); el aislamiento entre
tests se logra monkeypatcheando las rutas de estado (DB_PATH, OUTPUTS_DIR)
y limpiando los diccionarios en memoria documentados en la Fase 0
(_LOGIN_RATE, _USER_LAST_SEEN).

Importante (hallazgo de este mismo Paso 3): palbe_db.py:1479 llama a
init_db() de forma INCONDICIONAL al final del modulo, en el momento de
importarlo -- no dentro de ninguna funcion. En un despliegue real esto es
correcto, porque PALBE_DB_PATH se exporta ANTES de arrancar el proceso
Python, asi que DB_PATH ya lo recoge en el import. Pero si el test
importase el modulo primero y parcheara DB_PATH despues, init_db()
correria contra la ruta por defecto (junto al codigo) antes de que el
parche exista, dejando un palbe.db real y poblado en modules/palbe/ --
no el bug vacio de las cinco conexiones sueltas del Paso 2, sino un
efecto secundario de la propia metodologia de test. Por eso las variables
de entorno se fijan AQUI, antes de importar palbe_db/app_palbe_4 por
primera vez, igual que las fijaria Docker antes de lanzar uvicorn.

palbe_email.py debe existir junto al codigo para que app_palbe_4 importe
(su eliminacion real es el Paso 4, junto con el import). Si falta, todos
los tests fallan con ModuleNotFoundError -- sintoma, no bug del arnes.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
CODE_DIR = TESTS_DIR.parent
PLATAFORMA_SGE = CODE_DIR.parent.parent
DATOS_PRUEBA = PLATAFORMA_SGE / "datos_prueba"
SOURCE_DB = DATOS_PRUEBA / "palbe.db"
SOURCE_OUTPUTS = DATOS_PRUEBA / "outputs"

TEST_PASSWORD = "SoloParaPruebaLocal_2026!"

# Usuarios y proyectos reales (Fase 0 del plan) -- constantes para no repetir
# literales magicos por todo el arnes.
REAL_USERS = ["csuela", "ilasierra", "jmorales", "lluciani", "agarcia"]
ADMIN_USER = "csuela"
NON_ADMIN_USERS = ["ilasierra", "jmorales", "lluciani", "agarcia"]
TOTAL_PROJECTS = 9
ACTIVE_PROJECTS = 7
STX_AWS_PROJECT_ID = 4  # sin filas en project_users -- solo visible para admin

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

if not SOURCE_DB.exists():
    pytest.exit(f"Fixture no encontrado: {SOURCE_DB}. Ejecutar el Paso 1 primero.", returncode=1)

# Debe ir ANTES del primer "import palbe_db" / "import app_palbe_4" de mas
# abajo: son las variables que init_db() (llamado al importar palbe_db)
# leera para decidir donde crear el esquema la primera vez.
_SESSION_TMP_DIR = Path(tempfile.mkdtemp(prefix="palbe_pytest_session_"))
os.environ["PALBE_DB_PATH"] = str(_SESSION_TMP_DIR / "palbe.db")
os.environ["PALBE_OUTPUTS_DIR"] = str(_SESSION_TMP_DIR / "outputs")
os.environ["PALBE_BACKUP_DIR"] = str(_SESSION_TMP_DIR / "palbe_backups")

# El secreto del contexto va AQUI, antes de importar app_palbe_4, por el mismo
# motivo que las rutas de estado: palbe_contexto.register_routes() se llama en
# el import, asi que un monkeypatch posterior no puede hacer que la ruta
# exista. Se comprobo: sin esta linea, la ruta devuelve 404 y los tests fallan
# con un JSONDecodeError sobre un cuerpo vacio, que no apunta a la causa.
os.environ["PALBE_CONTEXTO_TOKEN"] = "secreto-de-contexto-de-prueba"

import atexit  # noqa: E402

atexit.register(shutil.rmtree, _SESSION_TMP_DIR, ignore_errors=True)

import palbe_auth  # noqa: E402
import palbe_db  # noqa: E402
import app_palbe_4 as app_module  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def test_password_hash():
    """bcrypt es lento: calcular el hash una sola vez por sesion de tests."""
    return palbe_auth.hash_password(TEST_PASSWORD)


@pytest.fixture()
def db_path(tmp_path, test_password_hash):
    """Copia de palbe.db real en un temporal por test. Las 5 contrasenas
    reales se sustituyen por TEST_PASSWORD -- SOLO en esta copia. El
    fixture compartido datos_prueba/palbe.db nunca se abre para escritura."""
    dest = tmp_path / "palbe.db"
    shutil.copy(SOURCE_DB, dest)
    conn = sqlite3.connect(dest)
    with conn:
        conn.execute("UPDATE users SET password_hash=?", (test_password_hash,))
    conn.close()
    return dest


@pytest.fixture()
def outputs_dir(tmp_path):
    dest = tmp_path / "outputs"
    if SOURCE_OUTPUTS.exists():
        shutil.copytree(SOURCE_OUTPUTS, dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)
    return dest


@pytest.fixture(autouse=True)
def _isolated_app_state(monkeypatch, db_path, outputs_dir):
    """Redirige TODA la app (palbe_db.DB_PATH + los sitios de app_palbe_4
    que usan _DB_PATH / OUTPUTS_DIR) al fixture temporal de este test, y
    limpia el estado en memoria compartido entre requests (Fase 0:
    _LOGIN_RATE, _USER_LAST_SEEN) para que un test no contamine al
    siguiente."""
    monkeypatch.setattr(palbe_db, "DB_PATH", str(db_path))
    monkeypatch.setattr(app_module, "_DB_PATH", str(db_path))
    monkeypatch.setattr(app_module, "OUTPUTS_DIR", outputs_dir)
    # db_path es una copia CRUDA de datos_prueba/palbe.db (foto real de
    # cliente), anterior a sso_user_mapping (Fase 4c): sin crearla aqui, la
    # tabla no existiria en la copia de ningun test. NO se llama a
    # palbe_db.init_db() para esto: ademas del CREATE TABLE, init_db()
    # reejecuta migraciones ALTER TABLE de otras tablas y, sobre todo,
    # dispara _backfill_from_meta() contra modules/palbe/outputs calculado
    # desde __file__ -- NO el outputs_dir aislado que esta fixture parchea
    # dos lineas arriba. Ese directorio esta en .gitignore: si alguien tiene
    # *_meta.json de entrenamientos locales ahi, el backfill haria
    # UPDATE/INSERT en iterations/iteration_params/iteration_features de la
    # copia de cada uno de los 56 tests con contenido no versionado y
    # distinto por maquina -- justo la clase de "funciona en mi maquina" que
    # este proyecto ya sufrio una vez. Por eso el DDL puntual: determinista,
    # no toca disco fuera del temporal del test, y visible aqui mismo.
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sso_user_mapping (
                keycloak_sub      TEXT PRIMARY KEY,
                palbe_user_id     INTEGER NOT NULL REFERENCES users(id),
                keycloak_username TEXT,
                keycloak_email    TEXT,
                linked_at         TEXT NOT NULL,
                linked_by         INTEGER
            )
        """)
        # Y la columna visto_en de active_context_user (2026-09-18, la fecha de
        # la franja del recibidor). Mismo criterio que el CREATE de arriba: DDL
        # puntual en vez de init_db(), por el backfill que explica el
        # comentario largo. En el servidor la anade init_db al arrancar el
        # contenedor; aqui la copia es cruda y hay que ponerla.
        try:
            conn.execute("ALTER TABLE active_context_user ADD COLUMN visto_en TEXT")
        except sqlite3.OperationalError:
            pass  # ya estaba: la copia puede venir de una base ya migrada
        # Y keycloak_name en sso_user_mapping (2026-09-18, el nombre de
        # persona para el bloque "Trabajando ahora"). Misma razon que arriba:
        # la copia es cruda y en el servidor la anade init_db al arrancar.
        try:
            conn.execute("ALTER TABLE sso_user_mapping ADD COLUMN keycloak_name TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    conn.close()
    app_module._LOGIN_RATE.clear()
    app_module._USER_LAST_SEEN.clear()
    yield


@pytest.fixture()
def client():
    """IP simulada fija (127.0.0.1) -- comparte _LOGIN_RATE dentro del test."""
    return TestClient(app_module.app, client=("127.0.0.1", 12345))


@pytest.fixture()
def other_ip_client():
    """Segunda IP simulada, para probar que el rate limit es por IP."""
    return TestClient(app_module.app, client=("10.0.0.99", 54321))


def login_as(test_client, username, password=TEST_PASSWORD, next_url="/"):
    return test_client.post(
        "/login",
        data={"username": username, "password": password, "next": next_url},
        follow_redirects=False,
    )


def _authenticated_client(test_client, username, password=TEST_PASSWORD):
    """Inicia sesion de verdad (HTTP, cookie firmada) y devuelve el mismo
    cliente, ya autenticado."""
    resp = login_as(test_client, username, password)
    assert resp.status_code == 303, f"login de {username} fallo: {resp.status_code} {resp.text[:200]}"
    # La cookie se llama "palbe_session" y no "session" (el default de
    # Starlette) desde que PALBE puede compartir host con el recibidor: los
    # dos usan SessionMiddleware, y con el mismo nombre se pisarian.
    assert "palbe_session" in resp.cookies
    return test_client


@pytest.fixture()
def logged_in_client(client):
    """Cliente con sesion de verdad ya iniciada (ADMIN_USER). Fase 4c: los
    tests de las rutas /sso/* que comprueban 404 con el flag apagado
    necesitan estar autenticados -- sin sesion, el middleware devuelve un
    303 a /login que enmascara el 404 (misma trampa que test_removed_routes.py)."""
    return _authenticated_client(client, ADMIN_USER)
