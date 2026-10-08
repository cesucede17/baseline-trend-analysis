"""
El contexto de usuario en PALBE: la ultima de las tres, y la delicada.

Contrato completo en la seccion «El contexto de usuario: lo que debe cumplir
cada herramienta» del runbook.

PALBE va al final y con la huella minima porque el monolito esta CONGELADO:
dos lineas en app_palbe_4.py y una funcion de lectura en palbe_db.py, igual
que entro el SSO. Nada de esquema nuevo, nada de escrituras.

Y lleva dos avisos que no tienen las otras dos, los dos con su test aqui:

1. **El middleware de PALBE escribe.** `_AuthMiddleware` llama a
   `_touch_user_seen()` en toda peticion que no este en `_PUBLIC_PATHS`, y eso
   alimenta el "Online ahora" del panel de admin. Como el recibidor pregunta
   en CADA carga de su portada, una ruta no declarada publica dejaria a las
   siete personas permanentemente conectadas -- falseado, y sin que nada
   falle.
2. **`get_active_context()` no recibe el usuario**: lo lee de un ContextVar
   cuyo `_active_ctx_key()` devuelve 0 si no se ha fijado. Olvidar
   `set_current_user_id()` no da error, da un contexto vacio para todo el
   mundo.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

RUTA = "/api/plataforma/contexto"
CABECERA = "X-SGE-Plataforma"
TOKEN = "secreto-de-contexto-de-prueba"
SUB = "sub-de-csuela"

# csuela es admin en la base de prueba; ilasierra y jmorales, usuarios.
YO, COMPA, AJENO = 1, 2, 3


def _hace(**kw):
    return (datetime.now() - timedelta(**kw)).isoformat()


@pytest.fixture(autouse=True)
def _con_secreto(monkeypatch):
    monkeypatch.setenv("PALBE_CONTEXTO_TOKEN", TOKEN)


@pytest.fixture
def base(db_path):
    """Escribe directamente en la COPIA de la base: lo que se prueba es la
    lectura. Devuelve un ayudante para sembrar cada caso."""
    def sembrar(*, proyecto_de=YO, miembros=(YO,), contexto_de=YO,
                paso=4, modo="train", iteraciones=()):
        conn = sqlite3.connect(str(db_path))
        try:
            # Idempotente: hay tests que siembran varias veces en bucle para
            # recorrer los seis pasos del asistente.
            conn.execute("DELETE FROM iterations WHERE session_id=900")
            conn.execute("DELETE FROM sessions WHERE id=900")
            conn.execute("DELETE FROM project_users WHERE project_id=900")
            conn.execute("DELETE FROM projects WHERE id=900")
            conn.execute("INSERT OR REPLACE INTO sso_user_mapping "
                         "(keycloak_sub, palbe_user_id, linked_at) VALUES (?,?,?)",
                         (SUB, YO, _hace(days=30)))
            conn.execute("INSERT OR REPLACE INTO projects (id, name, created_at, owner_id, is_active) "
                         "VALUES (900, 'Proyecto de prueba', ?, ?, 1)",
                         (_hace(days=20), proyecto_de))
            for uid in miembros:
                conn.execute("INSERT OR REPLACE INTO project_users "
                             "(project_id, user_id, role, added_at) VALUES (900,?,?,?)",
                             (uid, "member", _hace(days=20)))
            conn.execute("INSERT OR REPLACE INTO sessions (id, project_id, name, created_at, user_id) "
                         "VALUES (900, 900, 'Sesion de prueba', ?, ?)",
                         (_hace(days=10), proyecto_de))
            for i, (uid, cuando) in enumerate(iteraciones, start=900):
                conn.execute("INSERT OR REPLACE INTO iterations (id, session_id, name, created_at, user_id) "
                             "VALUES (?, 900, ?, ?, ?)", (i, f"it-{i}", cuando, uid))
            conn.execute("INSERT OR REPLACE INTO active_context_user "
                         "(user_id, project_id, session_id, iteration_id, mode, step) "
                         "VALUES (?, 900, 900, NULL, ?, ?)", (contexto_de, modo, paso))
            conn.commit()
        finally:
            conn.close()

    return sembrar


def _pedir(client, sub=SUB, token=TOKEN):
    cabeceras = {CABECERA: token} if token is not None else {}
    params = {"sub": sub} if sub is not None else {}
    return client.get(RUTA, params=params, headers=cabeceras,
                      follow_redirects=False)


# --- Los dos avisos propios de PALBE --------------------------------------


def test_la_ruta_es_publica_y_no_pasa_por_el_middleware(client, base):
    """Si no estuviera en _PUBLIC_PATHS, el middleware devolveria un 303 a
    /login a una ruta de maquina. Un 303 aqui significa que alguien la ha
    sacado de la lista."""
    base()

    r = _pedir(client)

    assert r.status_code == 200, (
        "la ruta no esta en _PUBLIC_PATHS: el middleware la ha interceptado"
    )


def test_preguntar_el_contexto_no_pone_a_nadie_como_conectado(client, base):
    """EL test de esta tarea.

    `_touch_user_seen()` alimenta el "Online ahora" del panel de admin, y el
    recibidor pregunta esto en CADA carga de su portada. Si la ruta lo
    disparara, las siete personas apareceran siempre conectadas: falseado de
    forma permanente, sin un error, sin una linea de log y sin ningun test
    que lo viera. Este."""
    import app_palbe_4

    base()
    app_palbe_4._USER_LAST_SEEN.clear()

    _pedir(client)
    _pedir(client)

    assert app_palbe_4._USER_LAST_SEEN == {}, (
        "la ruta de contexto ha marcado actividad: el 'Online ahora' del "
        "panel de admin quedaria falseado en cada carga de la portada"
    )


def test_la_ruta_no_escribe_en_la_base(client, base, db_path):
    """Ni auditoria, ni contexto activo, ni nada. Es de solo lectura."""
    base()

    def foto():
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute(
                "SELECT (SELECT COUNT(*) FROM audit_log),"
                "       (SELECT COUNT(*) FROM users),"
                "       (SELECT COUNT(*) FROM iterations),"
                "       (SELECT group_concat(user_id||':'||COALESCE(step,'')) "
                "        FROM active_context_user)"
            ).fetchone()
        finally:
            conn.close()

    antes = foto()
    _pedir(client)
    _pedir(client)

    assert foto() == antes


# --- La puerta -------------------------------------------------------------


def test_sin_el_secreto_no_se_contesta(client, base):
    base()
    assert _pedir(client, token=None).status_code == 401


def test_con_un_secreto_equivocado_no_se_contesta(client, base):
    base()
    malo = _pedir(client, token="otro-secreto")
    sin = _pedir(client, token=None)

    assert malo.status_code == 401
    assert malo.json() == sin.json()


def test_sin_sub_es_un_400(client, base):
    base()
    assert _pedir(client, sub=None).status_code == 400


def test_la_ruta_no_existe_sin_secreto_configurado():
    """Regla 9: vacio = la ruta NO se registra. Sobre una app desnuda, porque
    la de verdad se importa una sola vez por proceso de pytest."""
    from fastapi import FastAPI

    import palbe_contexto

    desnuda = FastAPI()

    assert palbe_contexto.register_routes(desnuda, token="") is False
    assert not [r for r in desnuda.routes if getattr(r, "path", "") == RUTA]


# --- Lo que contesta -------------------------------------------------------


def test_un_sub_sin_vincular_no_tiene_contexto_y_no_crea_usuario(client, base, db_path):
    """PALBE NO auto-aprovisiona, al contrario que Tambora y Bartolo: tenia
    cinco usuarios con datos previos que habia que respetar. Quien no esta en
    sso_user_mapping no tiene contexto, y no se le crea nada."""
    base()

    def usuarios():
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            conn.close()

    antes = usuarios()
    r = _pedir(client, sub="sub-que-nadie-ha-vinculado")

    assert r.status_code == 200
    assert r.json()["contexto"] is None
    assert usuarios() == antes


def test_el_contexto_lleva_el_proyecto_y_el_paso(client, base):
    """OJO con la url: el paso 4 se sirve en `/step/3`, no en `/step/4`.
    Este test afirmaba `/step/4` y estaba CODIFICANDO el fallo que encontro
    la revision -- pasaba en verde porque los dos lados usaban el mismo
    numero equivocado. La tabla de verdad esta en `_step_links`."""
    base(paso=4)

    ctx = _pedir(client).json()["contexto"]

    assert ctx["titulo"] == "Proyecto de prueba"
    assert "paso 4 de 6" in ctx["detalle"]
    assert ctx["url"] == "/step/3"


def test_sin_contexto_activo_no_hay_pastilla(client, base, db_path):
    base()
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM active_context_user WHERE user_id=?", (YO,))
    conn.commit(); conn.close()

    assert _pedir(client).json()["contexto"] is None


# --- Los companeros -------------------------------------------------------


def test_sale_quien_ha_tocado_mi_proyecto(client, base):
    """La fecha es DERIVADA: active_context_user no tiene columna de tiempo,
    asi que sale del created_at de las iteraciones de esa persona en ese
    proyecto."""
    base(miembros=(YO, COMPA),
         iteraciones=[(COMPA, _hace(hours=2)), (YO, _hace(minutes=5))])

    ctx = _pedir(client).json()["contexto"]

    assert [c["nombre"] for c in ctx["companeros"]] == ["ilasierra"]


def test_quien_no_ha_tocado_nada_no_sale(client, base):
    """El aviso es "alguien puede pisarte". Ser miembro no basta."""
    base(miembros=(YO, COMPA))

    assert _pedir(client).json()["contexto"]["companeros"] == []


def test_no_se_ve_a_nadie_de_un_proyecto_al_que_ya_no_pertenezco(client, base, db_path):
    """El contexto activo es una foto vieja: puede apuntar a un proyecto del
    que te han sacado. Sin comprobarlo, una fila rancia filtraria quien anda
    ahora en un proyecto que ya no es tuyo.

    Se prueba con un usuario NO admin, porque los admin ven todos los
    proyectos por regla de PALBE (_can_access_project) y este contexto la
    respeta en vez de inventar otra."""
    base(proyecto_de=AJENO, miembros=(AJENO,), contexto_de=COMPA,
         iteraciones=[(AJENO, _hace(hours=1))])
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE sso_user_mapping SET palbe_user_id=? WHERE keycloak_sub=?",
                 (COMPA, SUB))
    conn.commit(); conn.close()

    assert _pedir(client).json()["contexto"] is None


def test_el_compose_pasa_el_secreto_al_contenedor():
    """Octava vez. Sin esta linea la ruta no se registra, PALBE arranca
    perfectamente -- y la franja del recibidor nunca lo incluye, sin un solo
    error que lo explique."""
    import re
    from pathlib import Path

    compose = (
        Path(__file__).resolve().parents[1] / "docker-compose.yml"
    ).read_text(encoding="utf-8")

    assert re.search(r"^\s+PALBE_CONTEXTO_TOKEN:\s*\S", compose, re.MULTILINE), (
        "PALBE_CONTEXTO_TOKEN no llega al contenedor: la franja del "
        "recibidor no incluira a PALBE, sin ningun error visible"
    )


# --- El mapeo de paso a ruta (hallazgo de la revision) --------------------


def test_cada_paso_lleva_a_SU_pagina_y_no_a_otra(client, base):
    """El fallo que encontro la revision, y no lo veia ningun test porque
    ambos lados usaban el mismo numero.

    En PALBE el numero de paso NO es el de la ruta: la tabla de verdad es
    `_step_links` en app_palbe_4.py, y dice que el paso 2 vive en
    /step/ipmvp, el 3 en /step/exploracion y el 4 en /step/3. Mandar a
    /step/2 llevaba a "Paso 2 - Analisis Claude", y /step/3 a "Depuracion",
    que es el paso 4. O sea: la mitad del asistente aterrizaba en la pagina
    equivocada -- y aterrizar en el sitio exacto es TODO el sentido de esto.
    """
    esperado = {
        1: "/step/1",
        2: "/step/ipmvp",
        3: "/step/exploracion",
        4: "/step/3",
        5: "/step/5",
        6: "/step/6",
    }
    for paso, url in esperado.items():
        base(paso=paso)
        assert _pedir(client).json()["contexto"]["url"] == url, f"paso {paso}"


def test_un_paso_que_no_es_del_asistente_lleva_a_la_portada(client, base):
    """`/step/4` (Estadisticas) escribe step=0, que no es ningun paso del
    asistente. Y manana puede aparecer otro numero. En esos casos se lleva a
    la portada de PALBE, que ya ensena el contexto activo, en vez de a una
    ruta inventada."""
    for paso in (0, 99, -1):
        base(paso=paso)
        assert _pedir(client).json()["contexto"]["url"] == "/", f"paso {paso}"


def test_no_se_ensena_paso_0_de_6(client, base):
    """Consecuencia del mismo hallazgo: el detalle interpolaba el paso en
    crudo, asi que la pastilla podia decir "paso 0 de 6" -- que no significa
    nada para quien lo lee."""
    base(paso=0)

    detalle = _pedir(client).json()["contexto"]["detalle"]

    assert "paso 0" not in detalle
    assert "Sesion de prueba" in detalle


def test_una_cabecera_con_acentos_no_da_un_500(client, base):
    """La ruta es publica, asi que cualquiera de la red puede llamarla.
    `secrets.compare_digest` lanza TypeError con cadenas no-ASCII, y Starlette
    decodifica las cabeceras como latin-1: un 'e' con acento en el secreto
    devolvia un 500 con traza en vez del 401 indistinguible que toca."""
    base()

    # En BYTES, no en str: httpx se niega a enviar una cabecera con
    # caracteres no-ASCII, asi que un str aqui falla en el CLIENTE y no
    # llega a probar nada. Lo que de verdad llega por la red es un byte
    # suelto que Starlette decodifica como latin-1, y eso si produce el str
    # no-ASCII que hacia reventar a compare_digest.
    acentuado = ("secreto-con-" + chr(0xE9)).encode("latin-1")
    r = client.get(RUTA, params={"sub": SUB},
                   headers={CABECERA: acentuado},
                   follow_redirects=False)

    assert r.status_code == 401


# --- La fecha: "cuando estuviste TU" -------------------------------------


def test_navegar_deja_constancia_de_cuando_estuviste(db_path):
    """`active_context_user` no tenia columna de tiempo, asi que no habia
    ninguna fecha honesta que ensenar: las unicas atadas a un proyecto eran
    la de su creacion (meses atras) o las iteraciones guardadas, que en los
    datos reales eran CERO. Medido en el servidor antes de decidir esto.

    Se anade la columna y la toca `set_active_context`, o sea la navegacion
    normal. La ruta de contexto sigue sin escribir nada."""
    import palbe_db

    palbe_db.set_current_user_id(1)
    palbe_db.set_active_context(project_id=900, step=1)

    ctx = palbe_db.get_active_context()

    assert ctx.visto_en, "navegar no dejo constancia de cuando"


def test_la_fecha_se_refresca_al_seguir_navegando(db_path):
    """Es "cuando estuviste", no "cuando entraste la primera vez"."""
    import time

    import palbe_db

    palbe_db.set_current_user_id(1)
    palbe_db.set_active_context(project_id=900, step=1)
    primera = palbe_db.get_active_context().visto_en
    time.sleep(0.01)
    palbe_db.set_active_context(step=2)

    assert palbe_db.get_active_context().visto_en > primera


def test_el_contexto_devuelve_la_fecha(client, base):
    base()
    import palbe_db

    palbe_db.set_current_user_id(YO)
    palbe_db.set_active_context(project_id=900, step=3)

    ctx = _pedir(client).json()["contexto"]

    assert ctx["visto_en"], "la ruta no devuelve la fecha"


def test_un_contexto_de_antes_de_la_columna_no_rompe_nada(client, base):
    """Las filas que ya existen en el servidor no tienen fecha, y no la
    tendran hasta que esa persona vuelva a navegar. La pastilla tiene que
    salir igual, sin fecha: el contrato la admite vacia."""
    base()

    ctx = _pedir(client).json()["contexto"]

    assert ctx is not None
    assert ctx["titulo"] == "Proyecto de prueba"
    assert ctx["visto_en"] is None


def test_una_base_sin_migrar_no_tumba_palbe(db_path, monkeypatch):
    """El fallo que descubrieron 30 tests en rojo, y que no era de la franja.

    `init_db()` anade la columna al ARRANCAR el contenedor, no al copiar un
    fichero. Una base sin migrar hacia que `get_active_context()` lanzara
    KeyError -- y esa funcion la llama CUALQUIER pagina de PALBE, no solo la
    ruta de contexto. O sea: un 500 en toda la aplicacion, no una pastilla de
    menos.

    El arnes anade la columna como la anade el servidor, asi que este test se
    la quita a proposito: es el unico que ejercita el camino tolerante."""
    import sqlite3

    import palbe_db

    conn = sqlite3.connect(str(db_path))
    with conn:
        # SQLite no sabe quitar una columna en versiones antiguas, asi que se
        # rehace la tabla con el esquema de ANTES del 2026-09-18.
        conn.execute("DROP TABLE active_context_user")
        conn.execute("""CREATE TABLE active_context_user (
            user_id INTEGER PRIMARY KEY, project_id INTEGER, session_id INTEGER,
            iteration_id INTEGER, mode TEXT DEFAULT 'train', step INTEGER DEFAULT 1)""")
        conn.execute("INSERT INTO active_context_user (user_id, step) VALUES (1, 2)")
    conn.close()

    palbe_db.set_current_user_id(1)
    ctx = palbe_db.get_active_context()

    assert ctx.step == 2
    assert ctx.visto_en is None


def test_abrir_la_portada_de_palbe_ya_deja_constancia(logged_in_client, db_path):
    """Decision del 2026-09-18. Antes solo escribia la fecha la navegacion por
    los pasos del asistente, asi que quien entraba en PALBE, miraba y se iba
    no dejaba constancia -- y la franja del recibidor salia con pastilla y sin
    fecha, que es como parecia roto.

    La portada ya sabe en que proyecto estas (lo lee para pintar la barra
    lateral), asi que registrar la visita ahi es honesto: estuviste."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute("UPDATE active_context_user SET visto_en=NULL")
    conn.close()

    r = logged_in_client.get("/")
    assert r.status_code == 200

    conn = sqlite3.connect(str(db_path))
    try:
        con_fecha = conn.execute(
            "SELECT COUNT(*) FROM active_context_user WHERE visto_en IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()

    assert con_fecha >= 1, "abrir la portada no dejo constancia"


# --- Quien esta trabajando ahora (incremento 2) --------------------------


RUTA_ACTIVOS = "/api/plataforma/activos"


def _activos(client, token=TOKEN):
    cabeceras = {CABECERA: token} if token is not None else {}
    return client.get(RUTA_ACTIVOS, headers=cabeceras, follow_redirects=False)


def test_los_activos_piden_secreto_igual_que_el_contexto(client, base):
    base()
    assert _activos(client, token=None).status_code == 401
    assert _activos(client, token="otro").status_code == 401


def test_la_ruta_de_activos_tambien_es_publica(client, base):
    """Si no estuviera en _PUBLIC_PATHS, el middleware devolveria un 303 -- y
    ademas marcaria actividad, que es justo el dato que esta ruta LEE. Se
    falsearia a si misma."""
    base()
    assert _activos(client).status_code == 200


def test_quien_ha_hecho_algo_en_palbe_sale_con_su_sub(client, base):
    """PALBE ya sabia quien esta dentro: `_USER_LAST_SEEN`, el mismo dato que
    alimenta su "Online ahora". Esta ruta lo LEE, no lo escribe.

    Devuelve el `sub` de Keycloak y no el id interno, porque es lo que permite
    al recibidor agrupar a la misma persona en varias herramientas."""
    import app_palbe_4

    base()
    app_palbe_4._USER_LAST_SEEN.clear()
    app_palbe_4._touch_user_seen(YO)

    activos = _activos(client).json()["activos"]

    assert [a["sub"] for a in activos] == [SUB]
    assert activos[0]["nombre"] == "csuela"
    assert activos[0]["visto_en"]


def test_quien_no_esta_vinculado_no_sale(client, base):
    """Sin fila en `sso_user_mapping` no hay sub, y sin sub el recibidor no
    puede agrupar. Se omite en vez de inventarse un identificador."""
    import app_palbe_4

    base()
    app_palbe_4._USER_LAST_SEEN.clear()
    app_palbe_4._touch_user_seen(COMPA)   # ilasierra no esta vinculado en base()

    assert _activos(client).json()["activos"] == []


def test_preguntar_por_los_activos_no_marca_actividad(client, base):
    """El mismo aviso que el contexto, y aqui es peor: esta ruta LEE el dato
    que el middleware escribiria. Si pasara por ahi, se falsearia a si misma
    -- preguntar quien esta dentro pondria a todo el mundo dentro."""
    import app_palbe_4

    base()
    app_palbe_4._USER_LAST_SEEN.clear()

    _activos(client)
    _activos(client)

    assert app_palbe_4._USER_LAST_SEEN == {}


def test_los_activos_no_escriben_en_la_base(client, base, db_path):
    import sqlite3

    base()

    def foto():
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute(
                "SELECT (SELECT COUNT(*) FROM audit_log), (SELECT COUNT(*) FROM users)"
            ).fetchone()
        finally:
            conn.close()

    antes = foto()
    _activos(client)

    assert foto() == antes


def test_los_activos_dan_el_nombre_de_keycloak_si_se_conoce(client, base, db_path):
    """Fallo visto en una prueba con dos personas: la misma salia como "cejemplo"
    en la lista y con su nombre completo en el recibidor. PALBE era la unica
    de las tres que no guardaba el nombre -- solo el usuario."""
    import sqlite3

    import app_palbe_4

    base()
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute("UPDATE sso_user_mapping SET keycloak_name=? WHERE keycloak_sub=?",
                     ("Carlos Ejemplo Pérez", SUB))
    conn.close()
    app_palbe_4._USER_LAST_SEEN.clear()
    app_palbe_4._touch_user_seen(YO)

    activos = _activos(client).json()["activos"]

    assert [a["nombre"] for a in activos] == ["Carlos Ejemplo Pérez"]


def test_sin_nombre_de_keycloak_se_manda_el_usuario(client, base):
    """Degradacion honesta: la columna se llena cuando cada persona vuelve a
    entrar por SSO, asi que los primeros dias habra quien no lo tenga."""
    import app_palbe_4

    base()
    app_palbe_4._USER_LAST_SEEN.clear()
    app_palbe_4._touch_user_seen(YO)

    assert _activos(client).json()["activos"][0]["nombre"] == "csuela"


def test_entrar_por_sso_guarda_el_nombre(db_path):
    """Se guarda al ENTRAR y no al vincular: vincular lo hace una persona a
    mano una sola vez, asi que una columna rellenada solo ahi se quedaria
    vacia para todos los que ya estaban."""
    import sqlite3

    import palbe_db

    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute("INSERT OR REPLACE INTO sso_user_mapping "
                     "(keycloak_sub, palbe_user_id, linked_at) VALUES (?,1,?)",
                     ("sub-x", "2026-09-01T00:00:00"))
    conn.close()

    palbe_db.recordar_nombre_keycloak("sub-x", "Marta Modelo García")

    assert palbe_db.subs_por_usuario()[1]["nombre"] == "Marta Modelo García"
