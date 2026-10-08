"""PALBE colgada de una ruta, sin tocar sus 128 URLs a mano.

Cuando las cinco herramientas comparten un solo nombre y se reparten por
ruta, PALBE deja de ser dueña de la raíz: `/step/3` ya no es suyo, y
`/static/css/palbe.css` tampoco. Falla en silencio -- esas peticiones no
llegan nunca a PALBE, así que no hay error en sus logs, solo una pantalla sin
estilos y con los enlaces rotos.

**Por qué esto es un middleware y no 128 ediciones.**

PALBE no tiene plantillas: genera su HTML dentro de `app_palbe_4.py`, y ahí
hay 63 atributos `href`/`src`/`action`, 46 llamadas a `fetch()` y 19
navegaciones, repartidas entre f-strings y cadenas normales. Las cadenas
normales llevan CSS dentro, con sus llaves; convertirlas a f-strings para
poder interpolar el prefijo rompería ese CSS de formas que ningún test vería.

Y sobre todo: `app_palbe_4.py` está CONGELADO. Su propia convención, escrita
ahí mismo para el SSO y para el contexto de usuario, es que la funcionalidad
vive en un módulo aparte y en el monolito queda **una línea**. Esto la sigue.

**Qué hace exactamente.** Reescribe, en las respuestas HTML, las rutas
absolutas que apuntan a PALBE, y la cabecera `Location` de las
redirecciones. Nada más: si la variable no está puesta ni se registra, y
entonces el coste es cero -- ni una comparación por petición.

**Dónde va en la pila.** DENTRO de GZip, o reescribiría bytes comprimidos.
GZip se registra el último y por tanto es el más externo, así que esto se
registra antes.

**Lo que NO cubre, y no hace falta:** los ficheros de `static/` y `assets/`
no tienen ni una ruta absoluta (medido), y las rutas que PALBE *declara* no
llevan prefijo porque Traefik lo quita antes de pasar la petición.
"""

from __future__ import annotations

import os
import re


def _normalizar(valor: str) -> str:
    """`palbe`, `/palbe` y `/palbe/` son la misma intención."""
    v = (valor or "").strip().strip("/")
    return f"/{v}" if v else ""


RUTA_BASE = _normalizar(os.environ.get("PALBE_RUTA_BASE", ""))


def _patron(prefijo: str) -> re.Pattern[str]:
    """Las tres formas en las que PALBE emite una ruta absoluta.

    `(?!/)` deja fuera `//servidor/...`, que es un enlace a otro sitio sin
    esquema y no una ruta propia. Y el prefijo se excluye explícitamente para
    que la reescritura sea idempotente: si algún día esto corriera dos veces,
    no saldría `/palbe/palbe/step/3`.
    """
    escapado = re.escape(prefijo.lstrip("/"))
    # Los DOS guardianes, y hacen falta los dos. El primero deja fuera
    # `//servidor/...`; el segundo, lo que ya lleva el prefijo. Con solo el
    # segundo, `href="//otro.servidor/x"` pasaba el filtro --porque lo que
    # sigue a la primera barra no es `palbe/`-- y acababa reescrito como
    # `href="/palbe//otro.servidor/x"`, o sea un enlace externo convertido en
    # una ruta interna que no existe.
    no_repetir = rf"(?!/)(?!{escapado}/)"
    return re.compile(
        # href="/x"  src='/x'  action="/x"
        r"""((?:href|src|action)\s*=\s*["'])/"""
        + no_repetir
        + r"""|"""
        # fetch('/x')  fetch("/x")  fetch(`/x`)
        r"""(fetch\(\s*["'`])/"""
        + no_repetir
        + r"""|"""
        # location.assign('/x')  location.replace('/x')  location.href = '/x'
        r"""(location\.(?:assign|replace)\(\s*["'`]|location\.href\s*=\s*["'`])/"""
        + no_repetir
    )


class PrefijoDeRuta:
    """Middleware ASGI puro: sin BaseHTTPMiddleware, a propósito.

    `BaseHTTPMiddleware` envuelve la respuesta en una tarea aparte y rompe el
    streaming; aquí hay descargas de informes y de ficheros tratados que no
    deben pasar por un buffer. Con ASGI puro se puede decidir por respuesta:
    el HTML se acumula para reescribirlo, y todo lo demás pasa de largo sin
    tocarse ni un byte.
    """

    def __init__(self, app, prefijo: str) -> None:
        self.app = app
        self.prefijo = prefijo
        self.patron = _patron(prefijo)

    def _reescribir(self, html: bytes) -> bytes:
        texto = html.decode("utf-8", "surrogateescape")
        nuevo = self.patron.sub(
            lambda m: (m.group(1) or m.group(2) or m.group(3)) + self.prefijo + "/",
            texto,
        )
        return nuevo.encode("utf-8", "surrogateescape")

    def _location(self, valor: str) -> str:
        """Una redirección a `/login` tiene que ir a `/palbe/login`."""
        if valor.startswith("/") and not valor.startswith("//") \
                and not valor.startswith(self.prefijo + "/") and valor != self.prefijo:
            return self.prefijo + valor
        return valor

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        estado: dict = {"tocar": False, "inicio": None, "trozos": []}

        async def enviar(mensaje):
            tipo = mensaje["type"]

            if tipo == "http.response.start":
                cabeceras = []
                es_html = False
                comprimido = False
                for nombre, valor in mensaje.get("headers", []):
                    n = nombre.decode("latin-1").lower()
                    if n == "content-type":
                        es_html = "text/html" in valor.decode("latin-1").lower()
                    elif n == "content-encoding":
                        comprimido = True
                    elif n == "location":
                        valor = self._location(valor.decode("latin-1")).encode("latin-1")
                    cabeceras.append((nombre, valor))
                mensaje = {**mensaje, "headers": cabeceras}

                # Comprimido no se toca: significa que alguien comprimio antes
                # de llegar aqui, y descomprimir para reescribir seria pasarse
                # de listo. La pila registra esto DENTRO de GZip justo para
                # que no pase.
                estado["tocar"] = es_html and not comprimido
                if estado["tocar"]:
                    # El inicio se retiene: hay que recalcular content-length
                    # despues de reescribir, y va en esta cabecera.
                    estado["inicio"] = mensaje
                    return
                return await send(mensaje)

            if tipo == "http.response.body" and estado["tocar"]:
                estado["trozos"].append(mensaje.get("body", b""))
                if mensaje.get("more_body", False):
                    return
                cuerpo = self._reescribir(b"".join(estado["trozos"]))
                inicio = estado["inicio"]
                cabeceras = [
                    (n, v) for n, v in inicio["headers"]
                    if n.decode("latin-1").lower() != "content-length"
                ]
                cabeceras.append((b"content-length", str(len(cuerpo)).encode("latin-1")))
                await send({**inicio, "headers": cabeceras})
                return await send({"type": "http.response.body", "body": cuerpo})

            return await send(mensaje)

        return await self.app(scope, receive, enviar)


def register_middleware(app) -> bool:
    """Registra el middleware, y solo si hay prefijo.

    Devuelve si se ha registrado, para que el monolito no tenga que saber
    nada. Mismo patrón que `palbe_contexto.register_routes`: sin
    configuración no se registra nada y la importación no tiene efecto.
    """
    if not RUTA_BASE:
        return False
    app.add_middleware(PrefijoDeRuta, prefijo=RUTA_BASE)
    return True
