"""
Visibilidad por proyecto: _can_access_project (app_palbe_4.py:185-192)
para los 5 usuarios reales sobre los 9 proyectos reales, incluido el caso
del proyecto 4 (STX-AWS) sin ninguna fila en project_users -- invisible
para todo no-admin, visible solo para el admin (Fase 0, hallazgo
documentado, NO se "arregla": es el estado esperado).

Tambien cubre las dos ramas de codigo divergentes que deciden que
proyectos ve cada usuario: el dashboard (:1596-1600) y la barra lateral
(:846-851).
"""
import sqlite3

import pytest

from conftest import ADMIN_USER, NON_ADMIN_USERS, STX_AWS_PROJECT_ID, TOTAL_PROJECTS


def _all_project_ids(db_path):
    conn = sqlite3.connect(db_path)
    ids = [r[0] for r in conn.execute("SELECT id FROM projects").fetchall()]
    conn.close()
    return ids


def _user_project_ids(db_path, username):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    uid = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
    ids = {
        r[0]
        for r in conn.execute(
            "SELECT project_id FROM project_users WHERE user_id=?", (uid,)
        ).fetchall()
    }
    conn.close()
    return uid, ids


def test_hay_9_proyectos_en_el_fixture(db_path):
    assert len(_all_project_ids(db_path)) == TOTAL_PROJECTS


def test_admin_accede_a_los_9_proyectos(db_path):
    import app_palbe_4 as app_module
    import palbe_db

    uid, _ = _user_project_ids(db_path, ADMIN_USER)
    user = palbe_db.get_user_by_id(uid)
    assert user.role == "admin"

    for pid in _all_project_ids(db_path):
        assert app_module._can_access_project(user, pid), (
            f"el admin debe acceder al proyecto {pid}, incluido STX-AWS sin membresias"
        )


@pytest.mark.parametrize("username", NON_ADMIN_USERS)
def test_no_admin_solo_accede_a_sus_proyectos_asignados(db_path, username):
    import app_palbe_4 as app_module
    import palbe_db

    uid, assigned = _user_project_ids(db_path, username)
    user = palbe_db.get_user_by_id(uid)
    assert user.role != "admin"

    for pid in _all_project_ids(db_path):
        expected = pid in assigned
        actual = app_module._can_access_project(user, pid)
        assert actual == expected, (
            f"{username} sobre proyecto {pid}: esperado acceso={expected}, obtenido={actual}"
        )


@pytest.mark.parametrize("username", NON_ADMIN_USERS)
def test_stx_aws_invisible_para_no_admin(db_path, username):
    """Estado esperado documentado en la Fase 0: STX-AWS (id=4) no tiene
    ninguna fila en project_users. No se 'arregla' -- este test protege
    contra arreglarlo por error en un futuro merge."""
    import app_palbe_4 as app_module
    import palbe_db

    uid, _ = _user_project_ids(db_path, username)
    user = palbe_db.get_user_by_id(uid)
    assert not app_module._can_access_project(user, STX_AWS_PROJECT_ID)


def test_stx_aws_visible_para_admin(db_path):
    import app_palbe_4 as app_module
    import palbe_db

    uid, _ = _user_project_ids(db_path, ADMIN_USER)
    user = palbe_db.get_user_by_id(uid)
    assert app_module._can_access_project(user, STX_AWS_PROJECT_ID)


def _project_link_present(html: str, project_id: int) -> bool:
    """True si aparece un enlace a /projects/{id}. Ancla en la comilla de
    cierre (":\"" o "'") para evitar dos falsos positivos: que el nombre de
    un proyecto sea subcadena de otro (p.ej. 'INTERQUIM' de 'INTERQUIM
    isa'), o que un id numerico sea prefijo de otro (p.ej. '2' de '24')."""
    return f'/projects/{project_id}"' in html or f"/projects/{project_id}'" in html


@pytest.mark.parametrize("username", NON_ADMIN_USERS)
def test_dashboard_no_admin_no_ve_proyectos_ajenos(client, db_path, username):
    """app_palbe_4.py:1596-1600: rama del dashboard."""
    from conftest import login_as

    _, assigned = _user_project_ids(db_path, username)
    login_as(client, username)

    resp = client.get("/")
    assert resp.status_code == 200

    ajenos = set(_all_project_ids(db_path)) - assigned
    for pid in ajenos:
        assert not _project_link_present(resp.text, pid), (
            f"{username} no deberia ver un enlace al proyecto ajeno {pid} en el dashboard"
        )


@pytest.mark.parametrize("username", NON_ADMIN_USERS)
def test_sidebar_no_admin_no_ve_proyectos_ajenos(client, db_path, username):
    """app_palbe_4.py:846-851: rama de la barra lateral -- codigo distinto
    al del dashboard, puede divergir si alguien lo toca sin mirar el otro."""
    from conftest import login_as

    _, assigned = _user_project_ids(db_path, username)
    login_as(client, username)

    # cualquier pagina autenticada renderiza la barra lateral via _page()
    resp = client.get("/")
    assert resp.status_code == 200

    ajenos = set(_all_project_ids(db_path)) - assigned
    for pid in ajenos:
        assert not _project_link_present(resp.text, pid), (
            f"{username} no deberia ver un enlace al proyecto ajeno {pid} en la barra lateral"
        )
