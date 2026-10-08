"""
Login propio: credenciales buenas, malas, usuario inactivo, y el rate
limit de 5 intentos / IP (app_palbe_4.py:1494-1517).

Comportamiento de hoy, sin cambios de codigo: estos tests deben pasar
tal cual, antes y despues del Paso 4.
"""
from conftest import ADMIN_USER, TEST_PASSWORD, _authenticated_client, login_as


def test_login_credenciales_correctas(client):
    resp = login_as(client, ADMIN_USER)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "palbe_session" in resp.cookies


def test_login_password_incorrecta(client):
    resp = login_as(client, ADMIN_USER, password="password-incorrecta")
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert "palbe_session" not in resp.cookies


def test_login_usuario_inexistente(client):
    resp = login_as(client, "usuario_que_no_existe")
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


def test_login_usuario_inactivo(client, db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("UPDATE users SET is_active=0 WHERE username=?", ("jmorales",))
    conn.close()

    resp = login_as(client, "jmorales")
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"], "un usuario desactivado no debe poder entrar"
    assert "palbe_session" not in resp.cookies


def test_dashboard_sin_sesion_redirige_a_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_rate_limit_bloquea_tras_5_fallos(client):
    for _ in range(5):
        login_as(client, ADMIN_USER, password="incorrecta")

    resp = login_as(client, ADMIN_USER, password=TEST_PASSWORD)  # credenciales buenas
    assert "palbe_session" not in resp.cookies, (
        "al sexto intento (aunque sean credenciales correctas) el rate limit "
        "de 5 fallos/60s debe seguir bloqueando"
    )
    assert "Demasiados" in resp.headers["location"]


def test_open_redirect_next_interno_permitido(client):
    resp = login_as(client, ADMIN_USER, next_url="/projects/1")
    assert resp.headers["location"] == "/projects/1"


def test_open_redirect_protocol_relative_bloqueado(client):
    """'//evil.com' empieza por '/' pero el navegador lo resuelve como
    protocol-relative -> https://evil.com. Es la base de un phishing con
    el dominio corporativo legitimo: el login autentica de verdad y luego
    redirige fuera."""
    resp = login_as(client, ADMIN_USER, next_url="//evil.com")
    assert resp.headers["location"] == "/"


def test_open_redirect_backslash_bloqueado(client):
    """Varios navegadores tratan '/\\evil.com' igual que '//evil.com'."""
    resp = login_as(client, ADMIN_USER, next_url="/\\evil.com")
    assert resp.headers["location"] == "/"


def test_rate_limit_es_por_ip_no_global(client, other_ip_client):
    """Documenta el hallazgo de la Fase 4b: request.client.host determina el
    cubo del rate limit. Sin FORWARDED_ALLOW_IPS, cada IP simulada tiene su
    propio cubo -- este test prueba la logica de _LOGIN_RATE en si, no el
    comportamiento detras de un proxy real (eso es Traefik + uvicorn config,
    fuera del alcance de un TestClient)."""
    for _ in range(5):
        login_as(client, ADMIN_USER, password="incorrecta")

    # la otra IP no deberia estar bloqueada
    resp = login_as(other_ip_client, ADMIN_USER, password=TEST_PASSWORD)
    assert "palbe_session" in resp.cookies, (
        "5 fallos desde 127.0.0.1 no deben bloquear el login desde 10.0.0.99"
    )


def test_sin_sesion_y_con_sso_la_raiz_manda_al_sso(client, monkeypatch):
    import palbe_sso

    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", True)
    resp = client.get("/projects/3", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/sso/login?next=/projects/3"


def test_un_desactivado_con_cookie_viva_tampoco_ve_el_formulario_propio(
    client, db_path, monkeypatch
):
    """El hueco que deja mirar solo al middleware. El middleware decide con
    `request.session["user_id"]`, que sigue en la cookie; `/outputs` decide
    con `get_current_user()`, que devuelve None tambien cuando el usuario
    esta DESACTIVADO (palbe_auth.py, hueco cerrado a conciencia). Asi que al
    desactivar a alguien con la pestana abierta, su siguiente clic pasa el
    middleware, llega a la ruta y aterriza en el formulario propio -- justo
    la pantalla que esta rama queria dejar de ensenar. Y si su cuenta nacio
    por SSO, ese formulario no puede funcionar nunca: callejon sin
    salida."""
    import sqlite3

    import palbe_sso

    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", True)
    _authenticated_client(client, "jmorales")

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("UPDATE users SET is_active=0 WHERE username=?", ("jmorales",))
    conn.close()

    resp = client.get("/outputs/grafico.png", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/sso/login?next=/outputs/grafico.png"


def test_require_auth_manda_al_sso_igual_que_el_middleware(monkeypatch):
    """El tercer redirector: `palbe_auth._redirect_to_login`. No lo usa hoy
    ninguna ruta, pero es API publica del modulo y mandaba al formulario
    propio por su cuenta. Con el SSO en pie manda donde manda todo lo
    demas."""
    from types import SimpleNamespace

    import palbe_auth
    import palbe_sso

    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", True)
    request = SimpleNamespace(url=SimpleNamespace(path="/projects/3"))

    resp = palbe_auth._redirect_to_login(request)

    assert resp.headers["location"] == "/sso/login?next=/projects/3"


def test_sin_sesion_y_sin_sso_la_raiz_sigue_mandando_al_formulario(client, monkeypatch):
    """La puerta de rescate: con Keycloak apagado, el login propio sigue
    siendo lo que ve quien llega sin sesion."""
    import palbe_sso

    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", False)
    resp = client.get("/projects/3", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/projects/3"


def test_salir_con_el_sso_en_pie_no_devuelve_al_formulario_propio(client, monkeypatch):
    """Salir de PALBE tiene que dejarte en el SSO, no en el login propio.

    El formulario propio es la puerta de RESCATE: se llega escribiendo
    `/login` a mano, para cuando Keycloak este caido. Salir no es eso --
    es el camino normal--, y devolver ahi a quien acaba de cerrar sesion le
    ofrece una contrasena que en la mayoria de los casos no tiene: la local
    de PALBE, que para las cuentas nacidas por SSO no existe, y para las
    demas es una aleatoria que nadie apunto.

    Este caso es el de una sesion que NO vino por SSO (sin `sso_id_token`):
    la del SSO se va por `url_de_cierre`, que la manda a Keycloak.
    """
    from conftest import ADMIN_USER, _authenticated_client
    import palbe_sso

    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", True)
    c = _authenticated_client(client, ADMIN_USER)

    resp = c.get("/logout", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/sso/login?next=/", resp.headers["location"]


def test_salir_sin_sso_sigue_dejandote_en_el_formulario(client, monkeypatch):
    """La otra mitad: con Keycloak apagado, salir deja donde siempre."""
    from conftest import ADMIN_USER, _authenticated_client
    import palbe_sso

    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", False)
    c = _authenticated_client(client, ADMIN_USER)

    resp = c.get("/logout", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/"


# ---------------------------------------------------------------------------
# Una peticion de fondo no arranca un login
# ---------------------------------------------------------------------------
#
# Encontrado probando en el navegador el 2026-09-25, y solo se ve ahi. La
# interfaz de PALBE consulta `/context` cada 5 segundos (app_palbe_4.py, el
# setInterval del pie). Mientras el SSO mandaba a `/login`, eso era inofensivo.
# Mandando al SSO deja de serlo: CADA consulta de fondo sin sesion arranca un
# flujo OIDC entero y **reescribe el `state`** en la cookie. Si esa pestana
# sigue viva mientras la persona teclea en Keycloak, el callback bueno vuelve
# con un `state` que ya no es el guardado:
#
#     MismatchingStateError: CSRF Warning! State not equal in request and response
#
# y la persona ve la pagina de rechazo y tiene que entrar otra vez. No es una
# carrera rara entre dos pestanas: basta UNA pestana abierta y cinco segundos.


def test_una_peticion_de_fondo_sin_sesion_no_arranca_el_flujo_sso(client, monkeypatch):
    """`fetch()` manda `Sec-Fetch-Mode: cors`. Eso no es navegar: se responde
    401 y se deja la cookie en paz."""
    import palbe_sso

    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", True)
    resp = client.get("/context", headers={"Sec-Fetch-Mode": "cors"},
                      follow_redirects=False)

    assert resp.status_code == 401
    assert "location" not in resp.headers


def test_navegar_sin_sesion_sigue_yendo_al_sso(client, monkeypatch):
    """La otra mitad: escribir la direccion en la barra SI arranca el login."""
    import palbe_sso

    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", True)
    resp = client.get("/projects/3", headers={"Sec-Fetch-Mode": "navigate"},
                      follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/sso/login?next=/projects/3"


def test_sin_la_cabecera_se_trata_como_navegacion(client, monkeypatch):
    """Quien no manda `Sec-Fetch-Mode` --curl, un cliente viejo, esta suite--
    se comporta como hasta ahora. En la duda, la conducta de siempre."""
    import palbe_sso

    monkeypatch.setattr(palbe_sso, "_RUTAS_REGISTRADAS", True)
    resp = client.get("/projects/3", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/sso/login?next=/projects/3"
