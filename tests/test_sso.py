"""
Fase 4c: la tabla que cruza identidades de Keycloak con usuarios de PALBE.

La regla que estos tests protegen es que NO se auto-provisiona: un sub sin
mapear devuelve None, y nunca crea un usuario.
"""
import sqlite3

import pytest


def test_la_tabla_sso_existe(db_path):
    conn = sqlite3.connect(db_path)
    fila = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sso_user_mapping'"
    ).fetchone()
    conn.close()
    assert fila is not None, (
        "la fixture _isolated_app_state deberia crear sso_user_mapping "
        "con su DDL puntual (db_path es una copia cruda de datos_prueba/palbe.db, "
        "anterior a esta tabla)"
    )


def test_init_db_crea_sso_user_mapping_en_una_base_nueva(tmp_path, monkeypatch):
    """A diferencia del test anterior, este SI ejecuta init_db() de verdad,
    contra una base vacia. Es el que de verdad vigila el esquema: si alguien
    borra el bloque CREATE TABLE sso_user_mapping de palbe_db.py, este test
    (y solo este) revienta."""
    import palbe_db

    nueva_db = tmp_path / "nueva.db"
    monkeypatch.setattr(palbe_db, "DB_PATH", str(nueva_db))

    palbe_db.init_db()

    conn = sqlite3.connect(nueva_db)
    fila = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sso_user_mapping'"
    ).fetchone()
    conn.close()
    assert fila is not None, "init_db() deberia crear sso_user_mapping en una base nueva"


def test_un_sub_sin_mapear_devuelve_none(db_path):
    import palbe_db

    assert palbe_db.get_sso_mapping("sub-que-no-existe") is None


def test_crear_y_recuperar_un_mapeo(db_path):
    import palbe_db

    usuario = palbe_db.get_user_by_username("csuela")
    palbe_db.create_sso_mapping(
        "sub-de-prueba-1234",
        usuario.id,
        keycloak_username="csuela",
        keycloak_email="test.user@example.com",
    )
    assert palbe_db.get_sso_mapping("sub-de-prueba-1234") == usuario.id


def test_el_sub_es_unico(db_path):
    import palbe_db

    usuario = palbe_db.get_user_by_username("csuela")
    palbe_db.create_sso_mapping("sub-repetido", usuario.id)
    with pytest.raises(sqlite3.IntegrityError):
        palbe_db.create_sso_mapping("sub-repetido", usuario.id)


def test_la_tabla_users_no_se_toca(db_path):
    """El contrato de la Fase 4c: la tabla users no cambia."""
    conn = sqlite3.connect(db_path)
    columnas = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    conn.close()
    assert columnas == {
        "id", "username", "email", "password_hash", "role",
        "color", "created_at", "is_active", "display_name",
    }


def test_sin_variables_no_hay_configuracion(monkeypatch):
    import palbe_sso

    for v in ("PALBE_SSO_ISSUER", "PALBE_SSO_CLIENT_ID",
              "PALBE_SSO_CLIENT_SECRET", "PALBE_SSO_REDIRECT_URI"):
        monkeypatch.delenv(v, raising=False)
    assert palbe_sso.load_config() is None


def test_con_todas_las_variables_hay_configuracion(monkeypatch):
    import palbe_sso

    monkeypatch.setenv("PALBE_SSO_ISSUER", "http://auth.sge.local:8080/realms/sge")
    monkeypatch.setenv("PALBE_SSO_CLIENT_ID", "palbe")
    monkeypatch.setenv("PALBE_SSO_CLIENT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PALBE_SSO_REDIRECT_URI", "http://palbe.sge.local:8080/sso/callback")
    cfg = palbe_sso.load_config()
    assert cfg is not None
    assert cfg.client_id == "palbe"
    assert cfg.metadata_url.endswith("/.well-known/openid-configuration")


def test_falta_una_variable_y_no_hay_configuracion(monkeypatch):
    """Configuracion a medias es peor que ninguna: falla al arrancar, no a mitad."""
    import palbe_sso

    monkeypatch.setenv("PALBE_SSO_ISSUER", "http://auth.sge.local:8080/realms/sge")
    monkeypatch.setenv("PALBE_SSO_CLIENT_ID", "palbe")
    monkeypatch.delenv("PALBE_SSO_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("PALBE_SSO_REDIRECT_URI", "http://palbe.sge.local:8080/sso/callback")
    assert palbe_sso.load_config() is None


def test_con_el_flag_apagado_las_rutas_no_existen(logged_in_client):
    """Con PALBE_SSO_ENABLED=0 (el defecto en tests), /sso/* no debe existir.

    Se comprueba AUTENTICADO: sin sesion el middleware devuelve 303 a /login
    y ese 303 enmascara el 404 -- misma trampa que en test_removed_routes.py.
    """
    for ruta in ("/sso/login", "/sso/callback"):
        resp = logged_in_client.get(ruta, follow_redirects=False)
        assert resp.status_code == 404, f"{ruta} no deberia existir con el flag apagado"


def test_el_registro_no_hace_nada_sin_configuracion(monkeypatch):
    import palbe_sso
    from fastapi import FastAPI

    monkeypatch.setattr(palbe_sso, "SSO_ENABLED", True)
    for v in ("PALBE_SSO_ISSUER", "PALBE_SSO_CLIENT_ID",
              "PALBE_SSO_CLIENT_SECRET", "PALBE_SSO_REDIRECT_URI"):
        monkeypatch.delenv(v, raising=False)
    app = FastAPI()
    assert palbe_sso.register_sso_routes(app) is False
    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/sso")]


def test_el_registro_anade_las_dos_rutas(monkeypatch):
    import palbe_sso
    from fastapi import FastAPI

    # register_sso_routes() de verdad deja _RUTAS_REGISTRADAS en True como
    # efecto secundario (variable de modulo, no de la fixture). Sin este
    # monkeypatch de vuelta, ese True se cuela en cualquier test posterior
    # que no lo pida -- el mismo riesgo que describe destino_sin_sesion.
    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", False)
    monkeypatch.setattr(palbe_sso, "SSO_ENABLED", True)
    monkeypatch.setenv("PALBE_SSO_ISSUER", "http://auth.sge.local:8080/realms/sge")
    monkeypatch.setenv("PALBE_SSO_CLIENT_ID", "palbe")
    monkeypatch.setenv("PALBE_SSO_CLIENT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PALBE_SSO_REDIRECT_URI", "http://palbe.sge.local:8080/sso/callback")
    app = FastAPI()
    assert palbe_sso.register_sso_routes(app) is True
    rutas = {r.path for r in app.routes if getattr(r, "path", "").startswith("/sso")}
    assert rutas == {"/sso/login", "/sso/callback"}


def test_las_rutas_sso_estan_en_la_lista_blanca():
    """Sin esto el middleware las redirige a /login antes de llegar al handler,
    y el baile OIDC nunca puede empezar."""
    import app_palbe_4

    assert "/sso/login" in app_palbe_4._PUBLIC_PATHS
    assert "/sso/callback" in app_palbe_4._PUBLIC_PATHS


# --- resolver_sesion_sso: lo que ocurre DESPUES del token, probado sin red ---
#
# Ronda de correccion 1: register_sso_routes cubria el andamiaje (flag,
# configuracion, registro, lista blanca) pero nada probaba la decision que
# de verdad importa de esta fase. resolver_sesion_sso recibe los claims ya
# obtenidos -- ni Keycloak ni un token real hacen falta para probarla.


def test_un_sub_mapeado_escribe_la_sesion_y_redirige(db_path):
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    usuario = palbe_db.get_user_by_username("csuela")
    palbe_db.create_sso_mapping("sub-mapeado-ok", usuario.id)

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, {"sub": "sub-mapeado-ok", "realm_access": {"roles": ["palbe", "admin"]}}
    )

    assert resp.status_code == 303
    assert request.session["user_id"] == usuario.id


def test_un_sub_sin_el_rol_palbe_devuelve_403_y_no_crea_usuario(db_path):
    """Era la garantia de la Fase 4c: un sub sin mapear NUNCA crea un
    usuario. Desde el 2026-09-22 si lo crea -- pero solo si el token trae el
    rol palbe. La garantia no desaparece, se estrecha: sin ese rol, sigue sin
    crearse nada."""
    from types import SimpleNamespace

    import palbe_sso

    conn = sqlite3.connect(db_path)
    filas_antes = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, _claims("sub-que-no-existe-nunca", roles=["tecnico"])
    )

    assert resp.status_code == 403
    assert "user_id" not in request.session

    conn = sqlite3.connect(db_path)
    filas_despues = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    assert filas_despues == filas_antes


def test_un_sub_vacio_o_ausente_devuelve_403(db_path):
    from types import SimpleNamespace

    import palbe_sso

    for claims in ({"sub": ""}, {}):
        request = SimpleNamespace(session={})
        resp = palbe_sso.resolver_sesion_sso(request, claims)
        assert resp.status_code == 403
        assert "user_id" not in request.session


def test_se_escriben_los_roles_de_realm_access_en_la_sesion(db_path):
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    usuario = palbe_db.get_user_by_username("csuela")
    palbe_db.create_sso_mapping("sub-con-roles", usuario.id)

    request = SimpleNamespace(session={})
    palbe_sso.resolver_sesion_sso(
        request,
        {"sub": "sub-con-roles", "realm_access": {"roles": ["palbe", "operador", "lector"]}},
    )

    assert request.session["sso_roles"] == ["palbe", "operador", "lector"]


def test_sin_sso_el_rol_no_cambia(db_path):
    """Login propio: la sesion no lleva sso_roles y el rol es el de la BD."""
    import palbe_auth, palbe_db

    usuario = palbe_db.get_user_by_username("csuela")
    assert usuario.role == "admin"

    class PeticionFalsa:
        session = {"user_id": usuario.id}

    assert palbe_auth.get_current_user(PeticionFalsa()).role == "admin"


def test_sso_sin_rol_admin_rebaja_al_admin(db_path):
    """El rol de Keycloak solo puede restringir."""
    import palbe_auth, palbe_db

    usuario = palbe_db.get_user_by_username("csuela")

    class PeticionFalsa:
        session = {"user_id": usuario.id, "sso_roles": ["tecnico"]}

    assert palbe_auth.get_current_user(PeticionFalsa()).role != "admin"


def test_sso_con_rol_admin_conserva_el_admin(db_path):
    import palbe_auth, palbe_db

    usuario = palbe_db.get_user_by_username("csuela")

    class PeticionFalsa:
        session = {"user_id": usuario.id, "sso_roles": ["admin", "tecnico"]}

    assert palbe_auth.get_current_user(PeticionFalsa()).role == "admin"


def test_sso_nunca_concede_admin_a_quien_no_lo_es(db_path):
    """Interseccion, nunca union: aunque Keycloak diga admin, si la BD no lo
    dice, no lo es. Es la garantia de que un claim mal configurado no puede
    conceder privilegios que hoy no existen."""
    import palbe_auth, palbe_db

    usuario = palbe_db.get_user_by_username("jmorales")
    assert usuario.role != "admin"

    class PeticionFalsa:
        session = {"user_id": usuario.id, "sso_roles": ["admin"]}

    assert palbe_auth.get_current_user(PeticionFalsa()).role != "admin"


def test_el_boton_sso_no_aparece_con_el_flag_apagado(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "/sso/login" not in resp.text


def test_el_formulario_propio_sigue_ahi(client):
    """El login propio no se retira NUNCA, con SSO o sin el."""
    resp = client.get("/login")
    assert 'name="username"' in resp.text
    assert 'name="password"' in resp.text


# --- C1: el camino SSO no comprobaba is_active ---
#
# Revision final de la Fase 4c. El login propio rechaza a un usuario
# desactivado (palbe_auth.authenticate). El callback SSO solo miraba el
# mapeo, nunca is_active: un usuario desactivado en PALBE pero aun no
# borrado en Keycloak seguia entrando por /sso/login con sesion completa.


def test_un_sub_mapeado_a_un_usuario_desactivado_devuelve_403(db_path):
    """Igual que el usuario no vinculado: no se dan pistas sobre si la
    cuenta existe. Y no debe escribirse user_id en la sesion."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    usuario = palbe_db.get_user_by_username("jmorales")
    palbe_db.create_sso_mapping("sub-usuario-desactivado", usuario.id)

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("UPDATE users SET is_active=0 WHERE id=?", (usuario.id,))
    conn.close()

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, {"sub": "sub-usuario-desactivado", "realm_access": {"roles": ["palbe", "admin"]}}
    )

    assert resp.status_code == 403
    assert "user_id" not in request.session


def test_get_current_user_con_sesion_de_usuario_desactivado_devuelve_none(db_path):
    """Cierra ademas un hueco preexistente: una cookie viva sobrevivia a la
    desactivacion, con login propio o con SSO."""
    import palbe_auth
    import palbe_db

    usuario = palbe_db.get_user_by_username("jmorales")

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("UPDATE users SET is_active=0 WHERE id=?", (usuario.id,))
    conn.close()

    class PeticionFalsa:
        session = {"user_id": usuario.id}

    assert palbe_auth.get_current_user(PeticionFalsa()) is None


# --- I1: sso_roles sobrevive a un login propio posterior ---
#
# Escenario: entras por SSO sin rol admin -> sso_roles=["tecnico"] en
# sesion. Sin cerrar sesion, entras por el formulario propio como admin.
# user_id se sobreescribe, sso_roles no: el admin navega como user.


def test_login_post_limpia_los_sso_roles_residuales(db_path):
    """Simula una sesion que ya lleva sso_roles sin admin (el residuo que
    dejaria un login SSO previo), hace un login propio como csuela, y
    comprueba que get_current_user devuelve rol admin -- es decir, que
    login_post limpio el residuo antes de escribir el nuevo user_id."""
    import asyncio
    from types import SimpleNamespace

    import palbe_auth
    import palbe_db
    from app_palbe_4 import login_post
    from tests.conftest import TEST_PASSWORD

    admin = palbe_db.get_user_by_username("csuela")
    session = {"user_id": 999, "sso_roles": ["tecnico"]}
    request = SimpleNamespace(session=session, client=None)

    asyncio.run(login_post(request, username="csuela", password=TEST_PASSWORD, next="/"))

    assert "sso_roles" not in session, "login_post deberia limpiar la sesion antes de escribir user_id"
    assert session["user_id"] == admin.id

    class PeticionFalsa:
        pass

    peticion = PeticionFalsa()
    peticion.session = session
    assert palbe_auth.get_current_user(peticion).role == "admin"


def test_resolver_sesion_sso_limpia_residuos_previos_de_la_sesion(db_path):
    """Mismo arreglo en el otro sentido: si la sesion ya llevaba algo (fijacion
    de sesion, un login propio previo sin cerrar), resolver_sesion_sso debe
    limpiarla antes de escribir user_id y sso_roles."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    usuario = palbe_db.get_user_by_username("csuela")
    palbe_db.create_sso_mapping("sub-limpia-residuo", usuario.id)

    request = SimpleNamespace(session={"residuo": "de-antes", "user_id": 999})
    resp = palbe_sso.resolver_sesion_sso(
        request, {"sub": "sub-limpia-residuo", "realm_access": {"roles": ["palbe", "admin"]}}
    )

    assert resp.status_code == 303
    assert "residuo" not in request.session
    assert request.session["user_id"] == usuario.id


# --- Tarea 6: cierre de sesion unico ---
#
# Sin esto, /logout solo borraba la cookie de PALBE: cerrar sesion en PALBE
# dejaria viva la de Keycloak, y el Dashboard (o cualquier otra herramienta
# tras el recibidor) seguiria abierto.


def test_construye_la_url_de_cierre_de_keycloak(monkeypatch):
    """El cierre unico: no basta con borrar la cookie."""
    import palbe_sso

    monkeypatch.setenv("PALBE_SSO_ISSUER", "http://auth.sge.local:8080/realms/sge")
    monkeypatch.setenv("PALBE_SSO_CLIENT_ID", "palbe")
    monkeypatch.setenv("PALBE_SSO_CLIENT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PALBE_SSO_REDIRECT_URI", "http://palbe.sge.local:8080/sso/callback")
    monkeypatch.setenv("PALBE_SSO_POST_LOGOUT_URI", "http://palbe.sge.local:8080/login")

    url = palbe_sso.url_de_cierre("un-id-token")
    assert url is not None
    assert url.startswith("http://auth.sge.local:8080/realms/sge/protocol/openid-connect/logout?")
    assert "id_token_hint=un-id-token" in url
    assert "post_logout_redirect_uri=" in url
    assert "client_id=palbe" in url


def test_sin_configuracion_no_hay_url_de_cierre(monkeypatch):
    """Con el SSO apagado, /logout sigue funcionando como siempre."""
    import palbe_sso

    for v in ("PALBE_SSO_ISSUER", "PALBE_SSO_CLIENT_ID", "PALBE_SSO_CLIENT_SECRET",
              "PALBE_SSO_REDIRECT_URI"):
        monkeypatch.delenv(v, raising=False)
    assert palbe_sso.url_de_cierre("x") is None


# --- Ronda de correccion 1 ---
#
# Hallazgo 1: Keycloak exige id_token_hint O client_id para validar
# post_logout_redirect_uri contra las URIs registradas. Sin ninguno de los
# dos, el end_session_endpoint real devuelve 400 en crudo -- probado contra
# el servidor. Mismo defecto ya encontrado y corregido para el cliente
# dashboard (platform/portal/backend/sso.py, test_logout_sin_id_token_incluye_client_id).
# client_id debe ir SIEMPRE, con o sin id_token.


def test_sin_id_token_la_url_de_cierre_incluye_client_id(monkeypatch):
    """Sesion SSO cuyo token no traia id_token: sin client_id, Keycloak
    devolveria 400 en vez de redirigir."""
    import palbe_sso

    monkeypatch.setenv("PALBE_SSO_ISSUER", "http://auth.sge.local:8080/realms/sge")
    monkeypatch.setenv("PALBE_SSO_CLIENT_ID", "palbe")
    monkeypatch.setenv("PALBE_SSO_CLIENT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PALBE_SSO_REDIRECT_URI", "http://palbe.sge.local:8080/sso/callback")
    monkeypatch.setenv("PALBE_SSO_POST_LOGOUT_URI", "http://palbe.sge.local:8080/login")

    url = palbe_sso.url_de_cierre("")
    assert url is not None
    assert "client_id=palbe" in url
    assert "id_token_hint" not in url


# --- Revision final del Dashboard ---
#
# Hallazgo: docker-compose.yml del servidor NO reenviaba
# PALBE_SSO_POST_LOGOUT_URI al contenedor (mismo defecto que la Fase 4c con
# las otras cuatro variables), y NINGUN test de la suite cubria esa rama:
# todos fijan PALBE_SSO_POST_LOGOUT_URI con monkeypatch.setenv, asi que
# ninguno prueba que pasa sin ella -- que es justo la configuracion que
# estaba desplegada.


def test_sin_post_logout_uri_la_url_de_cierre_lleva_client_id(monkeypatch):
    """El resto del SSO esta configurado, pero PALBE_SSO_POST_LOGOUT_URI no
    llego al proceso (el fallo real: docker-compose.yml no la reenviaba).

    load_config() no la exige -- no forma parte de SSOConfig, la lee
    url_de_cierre() directamente del entorno -- asi que la configuracion
    SSO sigue existiendo y /logout sigue construyendo una URL de cierre.
    Sin post_logout_redirect_uri, Keycloak cierra la sesion pero deja a
    quien cierra sesion en su propia pantalla en vez de devolverlo a
    PALBE: no hay redireccion de vuelta, pero al menos no es un 400 en
    crudo, porque client_id sigue yendo siempre."""
    import palbe_sso

    monkeypatch.setenv("PALBE_SSO_ISSUER", "http://auth.sge.local:8080/realms/sge")
    monkeypatch.setenv("PALBE_SSO_CLIENT_ID", "palbe")
    monkeypatch.setenv("PALBE_SSO_CLIENT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PALBE_SSO_REDIRECT_URI", "http://palbe.sge.local:8080/sso/callback")
    monkeypatch.delenv("PALBE_SSO_POST_LOGOUT_URI", raising=False)

    url = palbe_sso.url_de_cierre("un-id-token")
    assert url is not None
    assert url.startswith("http://auth.sge.local:8080/realms/sge/protocol/openid-connect/logout?")
    assert "client_id=palbe" in url
    assert "id_token_hint=un-id-token" in url
    # La ausencia se declara: no hay post_logout_redirect_uri en la URL,
    # que es la causa exacta de que Keycloak no sepa a donde devolver a
    # quien cierra sesion.
    assert "post_logout_redirect_uri" not in url


# --- Ronda de correccion 1 ---
#
# Hallazgo 2: ningun test de la suite llamaba al MANEJADOR /logout -- los
# de arriba prueban solo la funcion pura url_de_cierre(). Que test_login.py
# este en verde no dice nada sobre /logout: hace falta invocarlo de verdad,
# con una sesion de login propio y con una sesion SSO (con y sin id_token).
# Mismo patron que test_login_post_limpia_los_sso_roles_residuales: se
# invoca el handler directamente con una sesion falsa, sin pasar por
# TestClient (que exigiria firmar la cookie de sesion a mano).


@pytest.mark.parametrize("sso_en_pie, destino", [
    (False, "/login?next=/"),
    (True, "/sso/login?next=/"),
])
def test_logout_de_una_sesion_propia_no_toca_keycloak(db_path, monkeypatch,
                                                      sso_en_pie, destino):
    """La regresion que mas importa vigilar: una sesion de login propio
    (sin sso_id_token) no debe tocar Keycloak.

    Donde aterriza SI depende del SSO, y por eso el flag va fijado aqui y no
    heredado: `_RUTAS_REGISTRADAS` es global del modulo y otro test lo deja
    encendido, asi que la version anterior de este test --que afirmaba
    `/login` a secas-- pasaba o no segun el orden.

    Con el SSO en pie el destino es `/sso/login`, no el formulario propio.
    Ese formulario es la puerta de RESCATE, a la que se llega escribiendo
    `/login` a mano; salir es el camino normal, y dejar ahi a quien acaba de
    cerrar sesion le ofrece una contrasena que casi nadie tiene. Lo que este
    test sigue vigilando es lo de siempre: ninguno de los dos destinos es una
    URL de Keycloak."""
    import asyncio
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso
    from app_palbe_4 import logout

    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", sso_en_pie)
    usuario = palbe_db.get_user_by_username("csuela")
    request = SimpleNamespace(session={"user_id": usuario.id})

    resp = asyncio.run(logout(request))

    assert resp.status_code == 303
    assert resp.headers["location"] == destino
    assert "protocol/openid-connect/logout" not in resp.headers["location"]


def test_logout_de_una_sesion_sso_con_id_token_redirige_a_keycloak(db_path, monkeypatch):
    """Cierre UNICO: una sesion que vino por SSO debe cerrar tambien la
    sesion de Keycloak, no solo borrar la cookie de PALBE."""
    import asyncio
    from types import SimpleNamespace

    import palbe_db
    from app_palbe_4 import logout

    monkeypatch.setenv("PALBE_SSO_ISSUER", "http://auth.sge.local:8080/realms/sge")
    monkeypatch.setenv("PALBE_SSO_CLIENT_ID", "palbe")
    monkeypatch.setenv("PALBE_SSO_CLIENT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PALBE_SSO_REDIRECT_URI", "http://palbe.sge.local:8080/sso/callback")
    monkeypatch.setenv("PALBE_SSO_POST_LOGOUT_URI", "http://palbe.sge.local:8080/login")

    usuario = palbe_db.get_user_by_username("csuela")
    request = SimpleNamespace(session={"user_id": usuario.id, "sso_id_token": "un-id-token"})

    resp = asyncio.run(logout(request))

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith(
        "http://auth.sge.local:8080/realms/sge/protocol/openid-connect/logout?"
    )
    assert "id_token_hint=un-id-token" in location


def test_logout_de_una_sesion_sso_sin_id_token_incluye_client_id(db_path, monkeypatch):
    """Sesion SSO cuyo token no traia id_token (sso_id_token queda como
    ""): client_id es lo unico que evita que Keycloak devuelva un 400 en
    crudo en vez de redirigir."""
    import asyncio
    from types import SimpleNamespace

    import palbe_db
    from app_palbe_4 import logout

    monkeypatch.setenv("PALBE_SSO_ISSUER", "http://auth.sge.local:8080/realms/sge")
    monkeypatch.setenv("PALBE_SSO_CLIENT_ID", "palbe")
    monkeypatch.setenv("PALBE_SSO_CLIENT_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("PALBE_SSO_REDIRECT_URI", "http://palbe.sge.local:8080/sso/callback")
    monkeypatch.setenv("PALBE_SSO_POST_LOGOUT_URI", "http://palbe.sge.local:8080/login")

    usuario = palbe_db.get_user_by_username("csuela")
    request = SimpleNamespace(session={"user_id": usuario.id, "sso_id_token": ""})

    resp = asyncio.run(logout(request))

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "client_id=palbe" in location
    assert "id_token_hint" not in location


def test_un_usuario_sin_vincular_no_tiene_sub(db_path):
    import palbe_db

    usuario = palbe_db.get_user_by_username("ilasierra")
    assert palbe_db.get_sso_sub_de_usuario(usuario.id) is None


def test_un_usuario_vinculado_devuelve_su_sub(db_path):
    import palbe_db

    usuario = palbe_db.get_user_by_username("ilasierra")
    palbe_db.create_sso_mapping("sub-de-ilasierra", usuario.id)
    assert palbe_db.get_sso_sub_de_usuario(usuario.id) == "sub-de-ilasierra"


def _claims(sub, roles=("palbe",), username="nuevo", **extra):
    """Unos claims como los que deja authlib en token["userinfo"].

    OJO con el valor por defecto de `roles`: trae ya el rol "palbe", asi que
    unos claims sin tocar PASAN la puerta del rol sin decirlo. Un test que
    quiera probar un rechazo por rol DEBE pasar `roles=` explicito (una lista
    sin "palbe", o None/[] para el caso de la lista vacia); si no, el rol de
    regalo lo neutraliza en silencio y el test queda verde sin probar nada."""
    c = {"sub": sub, "preferred_username": username}
    if roles is not None:
        c["realm_access"] = {"roles": list(roles)}
    c.update(extra)
    return c


def _acciones(db_path, accion):
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action=?", (accion,)
    ).fetchone()[0]
    conn.close()
    return n


def test_sin_el_rol_palbe_no_se_da_de_alta(db_path):
    from types import SimpleNamespace

    import palbe_sso

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, _claims("sub-sin-rol", roles=["tecnico"], username="forastero")
    )

    assert resp.status_code == 403
    assert "user_id" not in request.session
    assert _acciones(db_path, "sso_denegado_sin_rol") == 1


def test_un_token_sin_roles_se_registra_como_configuracion_no_como_permiso(db_path):
    """Todo usuario real lleva al menos default-roles-sge. Una lista vacia
    significa que el mapeador del cliente no mete realm_access en el id_token
    -- y eso hay que poder distinguirlo de 'no tiene permiso', porque la
    pagina que ve el usuario es la misma."""
    from types import SimpleNamespace

    import palbe_sso

    for claims in (_claims("sub-a", roles=None), _claims("sub-b", roles=[])):
        request = SimpleNamespace(session={})
        resp = palbe_sso.resolver_sesion_sso(request, claims)
        assert resp.status_code == 403
        assert "user_id" not in request.session

    assert _acciones(db_path, "sso_roles_ausentes") == 2
    assert _acciones(db_path, "sso_denegado_sin_rol") == 0


def test_un_vinculo_existente_solo_se_salta_la_puerta_si_el_token_no_trae_roles(db_path):
    """La escotilla, y su limite. Quien ya esta vinculado entra aunque el
    token no traiga roles: es la red de seguridad del despliegue -- si el
    mapeador de Keycloak falla, la lista viene vacia para TODO el mundo, y
    eso no significa "nadie tiene permiso" sino "no se sabe", asi que los
    que ya estaban -- incluido quien tenga que arreglarlo -- siguen
    entrando.

    Pero la escotilla es solo esa. Si el token SI trae roles y "palbe" no
    esta entre ellos, la respuesta del mapeador es fiable: se lo han
    quitado. Y entonces se niega el paso aunque el vinculo exista, porque si
    no, quitar el rol en Keycloak no surtiria efecto jamas sobre quien ya
    entro una vez."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    usuario = palbe_db.get_user_by_username("csuela")
    palbe_db.create_sso_mapping("sub-veterano", usuario.id)

    # Sin realm_access en absoluto: la escotilla. Entra.
    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(request, {"sub": "sub-veterano"})
    assert resp.status_code == 303
    assert request.session["user_id"] == usuario.id

    # Con realm_access y sin "palbe": revocacion. No entra.
    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, {"sub": "sub-veterano", "realm_access": {"roles": ["tambora"]}}
    )
    assert resp.status_code == 403
    assert "user_id" not in request.session


def test_a_un_vinculado_al_que_le_quitaron_el_rol_se_le_niega_el_paso(db_path):
    """El escenario que obliga a mirar el rol SIEMPRE: alguien se va de la
    organizacion, conserva su cuenta de Keycloak, le quitan el rol `palbe`
    siguiendo el runbook -- y al dia siguiente NO entra, aunque su vinculo
    siga ahi con todos sus proyectos."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    usuario = palbe_db.get_user_by_username("ilasierra")
    palbe_db.create_sso_mapping("sub-del-que-se-fue", usuario.id)

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request,
        _claims("sub-del-que-se-fue", roles=["tecnico"], username="ilasierra"),
    )

    assert resp.status_code == 403
    assert "user_id" not in request.session
    assert _acciones(db_path, "sso_denegado_sin_rol") == 1
    assert _acciones(db_path, "login_sso") == 0
    # El vinculo NO se borra: quitar el rol niega el paso, no destruye nada.
    assert palbe_db.get_sso_mapping("sub-del-que-se-fue") == usuario.id
    # Y la entrada del registro apunta al usuario, que es lo que distingue
    # una revocacion (habia vinculo) de un rechazo del alta (no lo habia).
    conn = sqlite3.connect(db_path)
    fila = conn.execute(
        "SELECT resource_id FROM audit_log WHERE action='sso_denegado_sin_rol'"
    ).fetchone()
    conn.close()
    assert fila[0] == usuario.id


def test_un_usuario_que_ya_existe_se_vincula_solo_y_entra(db_path):
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    esperado = palbe_db.get_user_by_username("ilasierra")

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, _claims("sub-de-ilasierra", username="ilasierra")
    )

    assert resp.status_code == 303
    assert request.session["user_id"] == esperado.id
    assert palbe_db.get_sso_mapping("sub-de-ilasierra") == esperado.id
    assert _acciones(db_path, "sso_autovinculo") == 1


def test_el_cruce_no_resucita_a_un_usuario_desactivado(db_path):
    """Desactivar a alguien tiene que seguir significando algo. Y no se
    escribe NADA: ni vinculo, ni fila de usuario."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    usuario = palbe_db.get_user_by_username("jmorales")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("UPDATE users SET is_active=0 WHERE id=?", (usuario.id,))
    conn.close()

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, _claims("sub-de-jmorales", username="jmorales")
    )

    assert resp.status_code == 403
    assert "user_id" not in request.session
    assert palbe_db.get_sso_mapping("sub-de-jmorales") is None
    # `sso_login_inactive` se escribe desde dos sitios de palbe_sso.py (el
    # cruce por nombre del alta automatica, y la relectura del usuario ya
    # vinculado). Aqui no hay ambiguedad: no existe vinculo para este sub,
    # asi que la segunda rama es inalcanzable y la unica entrada posible es
    # la del cruce.
    assert _acciones(db_path, "sso_login_inactive") == 1


def test_un_nombre_que_ya_es_de_otra_identidad_no_se_roba(db_path):
    """Dos personas distintas en Keycloak con el mismo nombre de usuario en
    PALBE. La segunda NO hereda la ficha de la primera."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    usuario = palbe_db.get_user_by_username("lluciani")
    palbe_db.create_sso_mapping("sub-de-la-primera", usuario.id)

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, _claims("sub-de-la-segunda", username="lluciani")
    )

    assert resp.status_code == 403
    assert "user_id" not in request.session
    assert palbe_db.get_sso_mapping("sub-de-la-segunda") is None
    assert palbe_db.get_sso_mapping("sub-de-la-primera") == usuario.id
    assert _acciones(db_path, "sso_colision_nombre") == 1


def test_sin_nombre_de_usuario_no_se_inventa_ninguno(db_path):
    from types import SimpleNamespace

    import palbe_sso

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(request, _claims("sub-anonimo", username=""))

    assert resp.status_code == 403
    assert _acciones(db_path, "sso_sin_nombre_de_usuario") == 1


def test_alguien_nuevo_del_todo_entra_y_estrena_usuario(db_path):
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    assert palbe_db.get_user_by_username("vballestin") is None

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request,
        _claims("sub-de-vballestin", username="vballestin",
                email="colleague@example.com"),
    )

    assert resp.status_code == 303
    nuevo = palbe_db.get_user_by_username("vballestin")
    assert nuevo is not None
    assert request.session["user_id"] == nuevo.id
    assert palbe_db.get_sso_mapping("sub-de-vballestin") == nuevo.id
    assert nuevo.email == "colleague@example.com"
    assert _acciones(db_path, "sso_autoalta") == 1


def test_el_usuario_nuevo_nunca_es_admin(db_path):
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    palbe_sso.resolver_sesion_sso(
        SimpleNamespace(session={}),
        _claims("sub-aspirante", roles=["palbe", "admin"], username="aspirante"),
    )

    assert palbe_db.get_user_by_username("aspirante").role == "user"


def test_la_cuenta_nueva_no_abre_el_formulario_propio(db_path):
    """El password_hash de una cuenta nacida por SSO tiene que ser
    IMPOSIBLE de satisfacer. Si fuera el hash de la cadena vacia, cada alta
    automatica regalaria una cuenta sin contrasena por /login."""
    import palbe_auth
    import palbe_db
    import palbe_sso
    from types import SimpleNamespace

    palbe_sso.resolver_sesion_sso(
        SimpleNamespace(session={}), _claims("sub-sin-clave", username="sinclave")
    )

    for intento in ("", " ", "sinclave", palbe_sso._SIN_CONTRASENA_PROPIA):
        assert palbe_auth.authenticate("sinclave", intento) is None, (
            f"la contrasena {intento!r} no deberia abrir una cuenta de SSO"
        )
    assert palbe_db.get_user_by_username("sinclave") is not None


def test_dos_altas_no_comparten_color(db_path):
    """El color distingue a las personas en la leyenda de un proyecto
    compartido (app_palbe_4.py, project_overview). Dos altas seguidas con el
    mismo color dejarian esa leyenda sin sentido."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    palbe_sso.resolver_sesion_sso(
        SimpleNamespace(session={}), _claims("sub-uno", username="unouno"))
    palbe_sso.resolver_sesion_sso(
        SimpleNamespace(session={}), _claims("sub-dos", username="dosdos"))

    assert (palbe_db.get_user_by_username("unouno").color
            != palbe_db.get_user_by_username("dosdos").color)


def test_el_destino_depende_de_si_el_sso_esta_activo(monkeypatch):
    import palbe_sso

    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", False)
    assert palbe_sso.destino_sin_sesion("/projects/3") == "/login?next=/projects/3"

    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", True)
    assert palbe_sso.destino_sin_sesion("/projects/3") == "/sso/login?next=/projects/3"


def test_el_callback_devuelve_al_destino_guardado(db_path):
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    usuario = palbe_db.get_user_by_username("csuela")
    palbe_db.create_sso_mapping("sub-con-destino", usuario.id)

    request = SimpleNamespace(session={"sso_next": "/outputs/grafico.png"})
    resp = palbe_sso.resolver_sesion_sso(request, {"sub": "sub-con-destino"})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/outputs/grafico.png"
    assert "sso_next" not in request.session, "el destino no sobrevive a la sesion"


def test_un_destino_que_no_es_relativo_cae_en_la_portada(db_path):
    """Sin esto, /sso/login?next=https://malo.example seria un redirector
    abierto con el sello de la plataforma.

    Los cuatro ultimos casos (backslash, tab, salto de linea, retorno de
    carro, byte nulo) no son los tres que probaba la primera version de
    este test: "empieza por / y no por //" los deja pasar, y el navegador
    los normaliza a una URL protocol-relative (WHATWG: "\\" se convierte en
    "/" en un esquema especial, y los caracteres de control se eliminan al
    parsear). Por eso cada uno se comprueba primero contra el guardian
    viejo -- si el guardian viejo tambien lo bloqueara, el caso no estaria
    probando nada nuevo."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    usuario = palbe_db.get_user_by_username("csuela")
    palbe_db.create_sso_mapping("sub-destino-raro", usuario.id)

    def _guardian_viejo(candidato):
        # La _destino_seguro original (solo "empieza por / y no por //").
        # Sirve para demostrar que los casos de abajo son de verdad nuevos.
        if candidato.startswith("/") and not candidato.startswith("//"):
            return candidato
        return "/"

    malos = (
        "https://malo.example/x",
        "//malo.example/x",
        "javascript:alert(1)",
        "/\\malo.example/x",       # backslash -> el navegador lee //
        "/\t//malo.example/x",     # tab -> WHATWG lo elimina al parsear
        "/\n//malo.example/x",     # salto de linea -> mismo motivo
        "/\r//malo.example/x",     # retorno de carro -> idem
        "/\x00//malo.example/x",   # byte nulo -> cualquier ord() < 0x20
    )

    for malo in malos:
        if _guardian_viejo(malo) != "/":
            assert _guardian_viejo(malo) == malo, (
                f"{malo!r} deberia atravesar el guardian viejo sin cambios "
                "-- si no, este caso no prueba nada nuevo"
            )
        request = SimpleNamespace(session={"sso_next": malo})
        resp = palbe_sso.resolver_sesion_sso(request, {"sub": "sub-destino-raro"})
        assert resp.headers["location"] == "/", f"{malo!r} no deberia colarse"


def test_los_destinos_legitimos_no_se_rompen():
    """Cerrar el redirector abierto rompiendo los enlaces profundos seria
    cambiar un problema por otro: el next existe precisamente para que un
    enlace compartido entre companeros aterrice donde debe."""
    import palbe_sso

    for legitimo in ("/", "/projects/3", "/outputs/grafico.png",
                      "/projects/3?tab=datos&mes=2026-09"):
        assert palbe_sso._destino_seguro(legitimo) == legitimo


def test_dos_pestanas_a_la_vez_en_el_primer_acceso(db_path, monkeypatch):
    """Simula el entrelazado real: esta peticion lee "no hay vinculo", la
    otra pestana termina su alta entera, y cuando esta intenta escribir,
    choca. No puede acabar en un 500 -- las dos son la misma persona."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    lecturas = {"n": 0}
    get_real = palbe_db.get_sso_mapping

    def get_sso_mapping_con_carrera(sub):
        lecturas["n"] += 1
        if lecturas["n"] == 1:
            # La primera lectura ve el mundo ANTES de que la otra pestana
            # acabe. Justo despues, la otra pestana termina:
            palbe_db.create_sso_mapping(
                sub,
                palbe_db.create_user(
                    username="apurada",
                    password_hash=palbe_sso._SIN_CONTRASENA_PROPIA,
                    role="user",
                ).id,
                keycloak_username="apurada",
            )
            return None
        return get_real(sub)

    monkeypatch.setattr(palbe_db, "get_sso_mapping", get_sso_mapping_con_carrera)

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, _claims("sub-apurada", username="apurada")
    )

    assert resp.status_code == 303, "la pestana que pierde la carrera debe entrar igual"
    assert request.session["user_id"] == get_real("sub-apurada")

    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM users WHERE username='apurada'"
    ).fetchone()[0]
    conn.close()
    assert n == 1, "y no debe quedar un usuario duplicado"


def test_reencontrarse_con_el_propio_vinculo_no_es_colision(db_path, monkeypatch):
    """El arreglo de raiz en _alta_automatica: mismo username, usuario ya
    vinculado al MISMO sub que llega. No es una colision con una identidad
    ajena -- soy yo, otra pestana mia ya dejo el vinculo escrito."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    usuario = palbe_db.get_user_by_username("ilasierra")
    palbe_db.create_sso_mapping("sub-de-ilasierra", usuario.id,
                                keycloak_username="ilasierra")

    get_real = palbe_db.get_sso_mapping
    lecturas = {"n": 0}

    def get_sso_mapping_primera_vez_en_blanco(sub):
        lecturas["n"] += 1
        if lecturas["n"] == 1:
            # Fuerza a resolver_sesion_sso a entrar en _alta_automatica
            # aunque el vinculo YA exista, para probar la cascada de verdad.
            return None
        return get_real(sub)

    monkeypatch.setattr(palbe_db, "get_sso_mapping", get_sso_mapping_primera_vez_en_blanco)

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, _claims("sub-de-ilasierra", username="ilasierra")
    )

    assert resp.status_code == 303
    assert request.session["user_id"] == usuario.id
    assert _acciones(db_path, "sso_colision_nombre") == 0


def test_el_except_de_integrity_error_entra_en_vez_de_propagar(db_path, monkeypatch):
    """Prueba el try/except sqlite3.IntegrityError en aislamiento: se fuerza
    el choque directamente en la escritura (create_user), con el vinculo YA
    presente en la base para este mismo sub -- para que la relectura DEL
    EXCEPT lo encuentre y la peticion entre en vez de propagar la
    excepcion como un 500."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    ganador = palbe_db.get_user_by_username("ilasierra")
    palbe_db.create_sso_mapping("sub-blindado", ganador.id,
                                keycloak_username="ilasierra")

    def create_user_que_choca(*args, **kwargs):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: users.username")

    monkeypatch.setattr(palbe_db, "create_user", create_user_que_choca)

    get_real = palbe_db.get_sso_mapping
    lecturas = {"n": 0}

    def get_sso_mapping_primera_vez_en_blanco(sub):
        lecturas["n"] += 1
        if lecturas["n"] == 1:
            # Fuerza a resolver_sesion_sso a entrar en _alta_automatica
            # aunque el vinculo YA exista para este sub.
            return None
        return get_real(sub)

    monkeypatch.setattr(palbe_db, "get_sso_mapping", get_sso_mapping_primera_vez_en_blanco)

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, _claims("sub-blindado", username="alguiennuevo")
    )

    assert resp.status_code == 303
    assert request.session["user_id"] == ganador.id
    assert lecturas["n"] == 2, (
        "debe pasar por el except (segunda lectura) y no llegar a la tercera"
    )


def test_la_relectura_sin_excepcion_atrapa_la_ventana_entre_las_dos_escrituras(
    db_path, monkeypatch
):
    """La segunda red, la que no depende de una excepcion: dentro de la
    rama "usuario nuevo" de _alta_automatica, create_user y
    create_sso_mapping son dos escrituras separadas. Esto simula que, justo
    cuando el except ya releyo y el vinculo TODAVIA no estaba escrito, la
    otra pestana termina de escribirlo -- y es esta SEGUNDA relectura,
    no el except, quien lo encuentra."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    ganador = palbe_db.create_user(
        username="ganadora-de-la-ventana",
        password_hash=palbe_sso._SIN_CONTRASENA_PROPIA,
        role="user",
    )

    def create_user_que_choca(*args, **kwargs):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: users.username")

    monkeypatch.setattr(palbe_db, "create_user", create_user_que_choca)

    get_real = palbe_db.get_sso_mapping
    lecturas = {"n": 0}

    def get_sso_mapping_con_ventana(sub):
        lecturas["n"] += 1
        if lecturas["n"] == 1:
            # Lectura de arriba del todo: fuerza la entrada en
            # _alta_automatica.
            return None
        if lecturas["n"] == 2:
            # La relectura DENTRO del except: el vinculo TODAVIA no esta
            # escrito -- la otra pestana termina de escribirlo justo
            # despues de esta lectura, no antes.
            palbe_db.create_sso_mapping(
                sub, ganador.id, keycloak_username="ganadora-de-la-ventana"
            )
            return None
        return get_real(sub)

    monkeypatch.setattr(palbe_db, "get_sso_mapping", get_sso_mapping_con_ventana)

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, _claims("sub-de-la-ventana", username="perdedora-de-la-ventana")
    )

    assert resp.status_code == 303
    assert request.session["user_id"] == ganador.id
    assert lecturas["n"] == 3, (
        "debe fallar en la relectura del except (n=2, vinculo aun no "
        "escrito) y encontrarlo en la relectura siguiente (n=3)"
    )


def test_el_cruce_por_nombre_no_distingue_mayusculas(db_path):
    """Keycloak manda `ilasierra` y en PALBE la ficha puede llamarse
    `ILasierra`. Sin comparar ignorando la caja, el paso 3 no la encuentra,
    el paso 4 crea una ficha nueva, y la persona acaba con dos usuarios y
    sus proyectos en el que ya no usa -- justo lo que el cruce existe para
    evitar. Y es silencioso: el registro diria `sso_autoalta`, que parece
    normal."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("UPDATE users SET username='ILasierra' WHERE username='ilasierra'")
    conn.close()
    esperado = palbe_db.get_user_by_username("ILasierra")

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, _claims("sub-de-ilasierra", username="ilasierra")
    )

    assert resp.status_code == 303
    assert request.session["user_id"] == esperado.id
    assert palbe_db.get_sso_mapping("sub-de-ilasierra") == esperado.id
    assert _acciones(db_path, "sso_autovinculo") == 1
    assert _acciones(db_path, "sso_autoalta") == 0
    # Y NO se ha estrenado una segunda ficha con la otra caja.
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM users WHERE username='ilasierra' COLLATE NOCASE"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_dos_fichas_que_solo_difieren_en_la_caja_son_una_colision(db_path):
    """Si mas de un usuario de PALBE coincide ignorando mayusculas no hay
    respuesta correcta: elegir una es repartir los proyectos a cara o cruz.
    Se trata como lo que es, una colision, y se niega sin escribir nada."""
    from types import SimpleNamespace

    import palbe_db
    import palbe_sso

    original = palbe_db.get_user_by_username("ilasierra")
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO users (username, email, password_hash, role, color, "
            "created_at, is_active) VALUES ('ILASIERRA', '', 'x', 'user', "
            "'#000000', '2026-09-23T00:00:00', 1)"
        )
    conn.close()

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, _claims("sub-ambiguo", username="ilasierra")
    )

    assert resp.status_code == 403
    assert "user_id" not in request.session
    assert palbe_db.get_sso_mapping("sub-ambiguo") is None
    assert _acciones(db_path, "sso_colision_nombre") == 1
    assert _acciones(db_path, "sso_autoalta") == 0
    assert _acciones(db_path, "sso_autovinculo") == 0
    assert palbe_db.get_sso_sub_de_usuario(original.id) is None


def test_la_pagina_de_rechazo_no_promete_un_remedio_que_no_existe(db_path):
    """Los tres rechazos ensenan la misma pagina a proposito, y por eso su
    texto no puede nombrar una causa. Lo que SI tiene que hacer es no
    mentir: desde que hay alta automatica, "no se crea ninguna cuenta
    automaticamente" es falso, y "contacta para que la vincule" manda a
    pedir algo que ya no existe. Y su enlace no puede devolver al
    formulario propio, que es el bucle del que nacio este trabajo."""
    from types import SimpleNamespace

    import palbe_sso

    request = SimpleNamespace(session={})
    resp = palbe_sso.resolver_sesion_sso(
        request, _claims("sub-rechazado", roles=["tecnico"], username="forastero")
    )
    pagina = resp.body.decode("utf-8")

    assert "vincule" not in pagina
    assert "No se crea ninguna cuenta" not in pagina
    assert 'href="/login"' not in pagina
    # Algo util que decirle al administrador: la hora, que es como se
    # encuentra la entrada del registro.
    assert "hora" in pagina.lower()
