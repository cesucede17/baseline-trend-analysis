"""PALBE colgada de una ruta.

PALBE no tiene plantillas: genera su HTML dentro de `app_palbe_4.py`, y ahí
hay 63 atributos `href`/`src`/`action`, 46 llamadas a `fetch()` y 19
navegaciones. Colgada de `/palbe`, todas apuntan un nivel por encima de donde
están las cosas, y falla en silencio: esas peticiones no llegan nunca a PALBE,
así que no hay error en sus logs -- solo una pantalla sin estilos y con los
enlaces rotos.

Se resuelve con un middleware (`palbe_rutas.py`) en vez de con 128 ediciones,
por dos motivos que están escritos ahí: las cadenas que no son f-strings
llevan CSS con llaves dentro y convertirlas rompería el CSS, y `app_palbe_4.py`
está congelado -- su convención, ya usada para el SSO y para el contexto de
usuario, es que la funcionalidad vive fuera y aquí queda una línea.

Un middleware que reescribe respuestas es una pieza con poder, así que se
prueba por los dos lados: que reescriba lo que debe, y que **no toque** lo que
no debe -- JSON, descargas, y lo que ya apunta a otro sitio.
"""
import importlib
import os

import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

import palbe_rutas


@pytest.mark.parametrize("dado,esperado", [
    ("", ""),
    ("palbe", "/palbe"),
    ("/palbe", "/palbe"),
    ("/palbe/", "/palbe"),
    ("  /palbe/  ", "/palbe"),
    ("/", ""),
])
def test_el_prefijo_se_normaliza(dado, esperado):
    assert palbe_rutas._normalizar(dado) == esperado


def test_sin_variable_no_se_registra_nada():
    """Coste cero cuando no se usa: ni una comparación por petición.

    Es lo que permite desplegar esto hoy, con la plataforma todavía por
    nombre, sin cambiar absolutamente nada del comportamiento.
    """
    modulo = importlib.reload(palbe_rutas)
    try:
        assert modulo.RUTA_BASE == ""
        app = Starlette()
        assert modulo.register_middleware(app) is False
        assert app.user_middleware == []
    finally:
        importlib.reload(palbe_rutas)


@pytest.fixture
def cliente(monkeypatch):
    """Una app mínima con el middleware puesto a `/palbe`.

    Mínima y no el monolito a propósito: lo que se prueba es la pieza, y así
    el test no arrastra base de datos, secretos ni SSO.
    """
    monkeypatch.setenv("PALBE_RUTA_BASE", "/palbe")
    modulo = importlib.reload(palbe_rutas)

    HTML = (
        '<link rel="stylesheet" href="/static/palbe.css">'
        "<a href='/step/3'>paso 3</a>"
        '<img src="/static/img/palbe.png">'
        '<form action="/login" method="post"></form>'
        '<a href="//otro.servidor/x">fuera</a>'
        '<a href="https://ejemplo.invalido/y">fuera</a>'
        "<script>"
        "fetch('/api/x'); fetch(\"/api/y\"); fetch(`/api/z`);"
        "location.assign('/step/1'); location.replace('/step/2');"
        "location.href = '/logout';"
        "</script>"
    )

    def pagina(_req):
        return HTMLResponse(HTML)

    def datos(_req):
        return JSONResponse({"ruta": "/api/x", "enlace": '<a href="/step/3">'})

    def descarga(_req):
        return Response(b'PK\x03\x04 href="/no-tocar"', media_type="application/zip")

    def salto(_req):
        return RedirectResponse("/login?next=/step/3", status_code=303)

    def salto_fuera(_req):
        return RedirectResponse("https://ejemplo.invalido/z", status_code=303)

    app = Starlette(routes=[
        Route("/pagina", pagina),
        Route("/datos", datos),
        Route("/descarga", descarga),
        Route("/salto", salto),
        Route("/salto-fuera", salto_fuera),
    ])
    assert modulo.register_middleware(app) is True
    try:
        yield TestClient(app)
    finally:
        monkeypatch.delenv("PALBE_RUTA_BASE", raising=False)
        importlib.reload(palbe_rutas)


@pytest.mark.parametrize("esperado", [
    'href="/palbe/static/palbe.css"',
    "href='/palbe/step/3'",
    'src="/palbe/static/img/palbe.png"',
    'action="/palbe/login"',
    "fetch('/palbe/api/x')",
    'fetch("/palbe/api/y")',
    "fetch(`/palbe/api/z`)",
    "location.assign('/palbe/step/1')",
    "location.replace('/palbe/step/2')",
    "location.href = '/palbe/logout'",
])
def test_reescribe_las_seis_formas(cliente, esperado):
    """Las tres comillas y los tres verbos. Cada una es una forma distinta de
    escribir lo mismo, y PALBE usa todas."""
    assert esperado in cliente.get("/pagina").text


def test_no_toca_lo_que_apunta_a_otro_sitio(cliente):
    texto = cliente.get("/pagina").text
    assert 'href="//otro.servidor/x"' in texto
    assert 'href="https://ejemplo.invalido/y"' in texto


def test_no_queda_ninguna_ruta_absoluta_sin_prefijo(cliente):
    """La afirmación global, por si alguna forma se escapa de la lista."""
    import re
    texto = cliente.get("/pagina").text
    sueltas = re.findall(r"""(?:href|src|action)\s*=\s*["']/(?!palbe/)(?!/)""", texto)
    sueltas += re.findall(r"""fetch\(\s*["'`]/(?!palbe/)""", texto)
    assert sueltas == []


def test_el_content_length_cuadra_con_lo_reescrito(cliente):
    """Reescribir alarga el cuerpo. Si la cabecera se queda con la longitud
    vieja, el navegador corta la página por la mitad -- y el HTML truncado a
    veces se ve bien, que es lo peor que puede pasar."""
    r = cliente.get("/pagina")
    assert int(r.headers["content-length"]) == len(r.content)


def test_el_json_no_se_toca(cliente):
    """Un JSON con una ruta dentro es un DATO, no un enlace. Reescribirlo
    cambiaría la respuesta del API."""
    cuerpo = cliente.get("/datos").json()
    assert cuerpo["ruta"] == "/api/x"
    assert cuerpo["enlace"] == '<a href="/step/3">'


def test_una_descarga_no_se_toca(cliente):
    """Los informes y los ficheros tratados son binarios: un byte cambiado
    los corrompe."""
    assert cliente.get("/descarga").content == b'PK\x03\x04 href="/no-tocar"'


def test_la_redireccion_lleva_el_prefijo(cliente):
    r = cliente.get("/salto", follow_redirects=False)
    assert r.headers["location"] == "/palbe/login?next=/step/3"


def test_una_redireccion_a_otro_sitio_se_respeta(cliente):
    r = cliente.get("/salto-fuera", follow_redirects=False)
    assert r.headers["location"] == "https://ejemplo.invalido/z"


def test_reescribir_dos_veces_no_duplica_el_prefijo(monkeypatch):
    """Idempotencia. Hoy corre una vez, pero un prefijo duplicado da un 404
    que cuesta leer."""
    monkeypatch.setenv("PALBE_RUTA_BASE", "/palbe")
    modulo = importlib.reload(palbe_rutas)
    try:
        m = modulo.PrefijoDeRuta(None, "/palbe")
        una = m._reescribir(b'<a href="/step/3">')
        assert m._reescribir(una) == una == b'<a href="/palbe/step/3">'
    finally:
        monkeypatch.delenv("PALBE_RUTA_BASE", raising=False)
        importlib.reload(palbe_rutas)


# ── Y el cableado en el monolito, que es lo que puede perderse en un merge ──

FUENTE = open(
    os.path.join(os.path.dirname(__file__), "..", "app_palbe_4.py"), encoding="utf-8"
).read()


def test_el_monolito_registra_el_middleware_ANTES_de_gzip():
    """`add_middleware` es LIFO: lo registrado después queda por fuera. Si
    esto acabara fuera de GZip, reescribiría bytes comprimidos -- y el
    resultado es una página que el navegador no puede descomprimir."""
    i = FUENTE.index("palbe_rutas.register_middleware(app)")
    j = FUENTE.index("app.add_middleware(GZipMiddleware")
    assert i < j


def test_la_cookie_de_sesion_tiene_nombre_propio():
    """El default de Starlette es "session", y el recibidor usa el mismo
    middleware. Con un host por herramienta da igual; compartiendo host
    --que es a donde vamos-- el tarro de cookies es el mismo y cada una
    tiraría la del otro. Entrar en PALBE echaría del recibidor, en bucle."""
    assert 'session_cookie="palbe_session"' in FUENTE


def test_la_app_NO_lleva_root_path():
    """El fallo del 2026-09-30, y por que no se prueba aqui como en las otras.

    Se habia puesto `root_path=palbe_rutas.RUTA_BASE` al construir la app.
    root_path significa lo contrario de lo que hace falta: "el proxy me pasa
    la ruta ENTERA, con prefijo". Traefik hace lo opuesto, se lo quita
    (`StripPrefix`), asi que Starlette buscaba `/palbe/static/...` mientras
    solo le llegaba `/static/...` -- y no servia ni un recurso. Las paginas
    cargaban con su contenido y sin una sola hoja de estilo ni imagen, sin un
    solo error en los logs.

    Tambora y Bartolo prueban esto levantando la app de verdad y pidiendo un
    fichero. Aqui no: recargar este monolito en un test arrastra base de
    datos, secretos y SSO, y el remedio saldria mas caro que la enfermedad.
    Asi que se afirma sobre el codigo, que para esta invariante basta -- lo
    que importa es que nadie vuelva a escribir ese argumento.
    """
    import re
    linea = re.search(r"^app = FastAPI\(.*$", FUENTE, re.M)
    assert linea, "no encuentro la construccion de la app"
    assert "root_path" not in linea.group(0), (
        "la app lleva root_path. Con StripPrefix delante no sirve NADA: "
        "Starlette busca la ruta con prefijo y solo le llega sin el. "
        + linea.group(0)
    )
