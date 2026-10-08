"""
El enlace de vuelta a la Plataforma SGE, en la cabecera.

Lo que protegen estos tests: que exista un camino de vuelta al recibidor que
NO pase por cerrar sesion (hasta la Fase del acceso desde la LAN, salir de
PALBE era cerrar sesion o teclear la URL a mano), que no aparezca cuando
PALBE corre suelto, y que la variable llegue de verdad al contenedor.

Ese ultimo es el que mas falta hacia: este proyecto lleva TRES veces dejandose
una variable fuera del bloque `environment` del compose (cuatro de SSO en la
Fase 4c, PALBE_SSO_POST_LOGOUT_URI en la ronda del recibidor, y
SGE_BASE_DOMAIN en la del acceso). Ningun test de codigo lo ve, porque todos
fijan el entorno a mano -- que es justo lo que oculta el hueco.
"""
import re
from pathlib import Path

import pytest

PLATAFORMA = "http://sge.local:8080/"


def _cabecera(client):
    r = client.get("/")
    assert r.status_code == 200, f"el dashboard no responde: {r.status_code}"
    return r.text


def test_con_la_url_configurada_la_cabecera_lleva_el_enlace(
    monkeypatch, logged_in_client
):
    import app_palbe_4

    monkeypatch.setattr(app_palbe_4, "_PLATAFORMA_URL", PLATAFORMA)
    html = _cabecera(logged_in_client)

    assert 'class="plataforma-link"' in html
    assert f'href="{PLATAFORMA}"' in html
    assert "Plataforma SGE" in html


def test_sin_la_url_no_se_pinta_nada(monkeypatch, logged_in_client):
    """PALBE suelto -- un portatil de desarrollo -- no ensena un boton que no
    lleva a ningun sitio. Mismo criterio que el SSO."""
    import app_palbe_4

    monkeypatch.setattr(app_palbe_4, "_PLATAFORMA_URL", "")
    html = _cabecera(logged_in_client)

    assert "plataforma-link" not in html


def test_volver_no_es_cerrar_sesion(monkeypatch, logged_in_client):
    """Los dos caminos coexisten y son distintos. Si alguien "simplifica"
    esto apuntando el enlace a /logout, el boton deja de resolver el problema
    por el que se puso: irse del todo cuando solo querias cambiar de
    herramienta."""
    import app_palbe_4

    monkeypatch.setattr(app_palbe_4, "_PLATAFORMA_URL", PLATAFORMA)
    html = _cabecera(logged_in_client)

    enlace = re.search(r'<a href="([^"]*)" class="plataforma-link"', html)
    assert enlace, "no se encontro el enlace de vuelta"
    assert "/logout" not in enlace.group(1)
    # Y el de cerrar sesion sigue estando, que es lo otro que se puede querer.
    assert 'href="/logout"' in html


def test_la_url_se_escapa(monkeypatch, logged_in_client):
    """La URL viene del .env del servidor, no de un usuario -- pero acaba
    dentro de un atributo href, y eso se escapa siempre. Si no, una comilla
    cierra el atributo y lo que venga detras entra como markup."""
    import app_palbe_4

    monkeypatch.setattr(
        app_palbe_4, "_PLATAFORMA_URL", 'http://x/" onmouseover="alert(1)'
    )
    html = _cabecera(logged_in_client)

    assert 'onmouseover="alert(1)"' not in html
    assert "&quot;" in html or "&#x27;" in html


def test_el_compose_pasa_la_url_al_contenedor():
    """Sin esta linea el boton no existe y NADA falla al arrancar: la
    cabecera se pinta igual, solo que sin enlace. Hay que leer el compose,
    porque el resto de los tests fija la variable a mano."""
    compose = (
        Path(__file__).resolve().parent.parent / "docker-compose.yml"
    ).read_text(encoding="utf-8")

    # Una ASIGNACION, no la aparicion del texto: el fichero lleva un
    # comentario que nombra la variable, asi que buscar la cadena suelta
    # daria verde con la linea borrada.
    asignada = re.search(
        r"^\s+PALBE_PLATAFORMA_URL:\s*\S", compose, re.MULTILINE
    )
    assert asignada, (
        "PALBE_PLATAFORMA_URL no llega al contenedor: la cabecera de PALBE se "
        "quedaria sin camino de vuelta al recibidor, sin ningun error visible"
    )


def test_la_cabecera_sigue_teniendo_tres_hijos(monkeypatch, logged_in_client):
    """.topbar es un grid de TRES columnas (1fr auto 1fr) y 54px de alto.

    Un cuarto hijo directo se va a una segunda fila implicita y empuja hacia
    abajo la busqueda y el chip de usuario -- que es exactamente lo que paso
    al anadir el enlace de vuelta, y solo se vio en el navegador. De ahi que
    la vuelta viva DENTRO de .topbar-izq, junto a la marca.

    Este test cuenta hijos, no pixeles: no comprueba que se vea bien, pero si
    caza la causa concreta de que se rompa la fila.
    """
    from html.parser import HTMLParser

    import app_palbe_4

    monkeypatch.setattr(app_palbe_4, "_PLATAFORMA_URL", PLATAFORMA)
    html = _cabecera(logged_in_client)

    class Hijos(HTMLParser):
        VACIAS = {"img", "br", "hr", "input", "meta", "link", "path", "circle"}

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.dentro = False
            self.prof = 0
            self.hijos = []

        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            if not self.dentro and tag == "header" and "topbar" in (d.get("class") or ""):
                self.dentro = True
                return
            if self.dentro and tag not in self.VACIAS:
                if self.prof == 0:
                    self.hijos.append((tag, d.get("class", "")))
                self.prof += 1

        def handle_endtag(self, tag):
            if not self.dentro or tag in self.VACIAS:
                return
            if tag == "header" and self.prof == 0:
                self.dentro = False
                return
            self.prof -= 1

    p = Hijos()
    p.feed(html)

    assert len(p.hijos) == 3, (
        f"la cabecera tiene {len(p.hijos)} hijos directos y el grid solo "
        f"define 3 columnas, asi que la fila se parte: {p.hijos}"
    )
    # Y el primero es la columna izquierda, con la vuelta dentro.
    assert p.hijos[0][1] == "topbar-izq", p.hijos
