"""
Aislamiento de la ruta de BD (Paso 2, ya cerrado): formaliza como
regresion permanente los ciclos RED-GREEN manuales de esa fase.

app_palbe_4.py abria cinco conexiones SQLite propias, construyendo la
ruta con _HERE/"palbe.db" en vez de usar palbe_db.DB_PATH. Con
PALBE_DB_PATH apuntando a un volumen, esas cinco escribirian en una
segunda BD vacia junto al codigo, con el sintoma enganoso "no such
table". Se corrigieron en el Paso 2; este test es la red que impide
que alguien reintroduzca el bug sin darse cuenta.

Cubre por HTTP real las tres ramas mas representativas:
- /iterations/{id}/start-fresh   (:3584-3590)
- /iterations/{id}/modify        (:3603-3609)
- /step/3/confirm, rama _can_reuse (:5601-5616)

Las otras dos (step5_train _existing_it, y el tamano de BD en /admin) se
corrigieron con el mismo patron y se cubren por lectura de codigo, no por
ejecucion directa (documentado en el commit del Paso 2) -- exigirian un
entrenamiento ML real o acceso admin fuera del alcance de este test.
"""
from pathlib import Path

from conftest import ADMIN_USER, CODE_DIR, login_as


def _stray_db_path() -> Path:
    return CODE_DIR / "palbe.db"


def _huella_junto_al_codigo() -> tuple:
    """Existencia, tamano y mtime de palbe.db (y sus -wal/-shm) junto al codigo.

    Lo que se vigila es que la ACCION no escriba ahi, no que el fichero no
    exista: un arranque local sin PALBE_DB_PATH (o link_sso.py) lo crea a
    proposito, y eso no es el bug que busca este test. Se compara la huella
    de antes y despues de la accion."""
    huella = []
    for sufijo in ("", "-wal", "-shm"):
        ruta = CODE_DIR / f"palbe.db{sufijo}"
        if ruta.exists():
            st = ruta.stat()
            huella.append((sufijo, st.st_size, st.st_mtime_ns))
        else:
            huella.append((sufijo, None, None))
    return tuple(huella)


def _first_session_id(db_path) -> int:
    import sqlite3

    conn = sqlite3.connect(db_path)
    sid = conn.execute("SELECT id FROM sessions LIMIT 1").fetchone()[0]
    conn.close()
    return sid


def test_start_fresh_no_escribe_junto_al_codigo(client, db_path):
    login_as(client, ADMIN_USER)
    session_id = _first_session_id(db_path)

    import palbe_db

    it = palbe_db.add_iteration(session_id=session_id, name="_check_start_fresh", phase="train")

    antes = _huella_junto_al_codigo()
    resp = client.post(f"/iterations/{it.id}/start-fresh", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/step/1"

    assert _huella_junto_al_codigo() == antes, (
        f"start-fresh escribio en {_stray_db_path()} en vez de en PALBE_DB_PATH"
    )


def test_modify_no_escribe_junto_al_codigo(client, db_path):
    login_as(client, ADMIN_USER)
    session_id = _first_session_id(db_path)

    import palbe_db

    it = palbe_db.add_iteration(session_id=session_id, name="_check_modify", phase="train")

    antes = _huella_junto_al_codigo()
    resp = client.post(f"/iterations/{it.id}/modify", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/step/5?from_iter={it.id}"

    assert _huella_junto_al_codigo() == antes, (
        f"modify escribio en {_stray_db_path()} en vez de en PALBE_DB_PATH"
    )


def test_step3_confirm_can_reuse_actualiza_la_bd_correcta(client, db_path):
    """Ejercita la rama _can_reuse (:5601-5608): ctx.iteration_id debe
    apuntar a una iteracion existente, sin guardar, en phase='train', de
    la MISMA sesion activa."""
    login_as(client, ADMIN_USER)
    session_id = _first_session_id(db_path)

    import palbe_db

    it = palbe_db.add_iteration(session_id=session_id, name="_check_can_reuse", phase="train")

    antes = _huella_junto_al_codigo()
    # /context/activate pasa por el middleware real -> keyea por el
    # usuario autenticado en ESTA sesion HTTP, no por un id arbitrario.
    resp = client.post(
        "/context/activate",
        json={"session_id": session_id, "iteration_id": it.id},
    )
    assert resp.status_code == 200

    resp = client.post(
        "/step/3/confirm",
        json={"excluded_rows": [], "iter_name": "_check_can_reuse_renamed"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    it_after = palbe_db.get_iteration(it.id)
    assert it_after.name == "_check_can_reuse_renamed", (
        "la rama _can_reuse no actualizo la fila esperada -- si esto falla, "
        "sospechar que alguna de las 5 conexiones sueltas volvio a usar "
        "_HERE/BASE_DIR en vez de _DB_PATH"
    )
    assert _huella_junto_al_codigo() == antes, (
        f"step/3/confirm escribio en {_stray_db_path()} en vez de en PALBE_DB_PATH"
    )
