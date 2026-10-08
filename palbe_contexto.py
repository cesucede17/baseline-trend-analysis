"""
El contexto de usuario de PALBE: "en que andaba esta persona".

Cumple el contrato de la seccion «El contexto de usuario: lo que debe cumplir
cada herramienta» del runbook.

POR QUE ESTE FICHERO EXISTE, y por que es tan pequeno: `modules/palbe/` esta
CONGELADO -- solo admite correcciones. Asi que el contexto entra como entro el
SSO en la Fase 4c: un modulo aparte y autocontenido, con DOS lineas en
app_palbe_4.py (la ruta en `_PUBLIC_PATHS` y el registro) y UNA funcion de
lectura nueva en palbe_db.py. Ni una tabla, ni una migracion, ni una
escritura.

TRES COSAS QUE NO HACE, y las tres tienen un fallo concreto detras:

1. **No escribe NADA.** Ni `log_action`, ni `_touch_user_seen`, ni contexto
   activo. No es higiene: `_touch_user_seen` alimenta el "Online ahora" del
   panel de admin, y el recibidor pregunta esto en CADA carga de su portada.
   Una escritura aqui dejaria a las siete personas permanentemente
   conectadas, sin un error y sin una linea de log.

2. **No crea usuarios.** PALBE no auto-aprovisiona, al contrario que Tambora
   y Bartolo: tenia cinco usuarios con proyectos y datos previos que habia
   que respetar. Quien no esta en `sso_user_mapping` no tiene contexto, y
   punto.

3. **No devuelve urls absolutas.** Dentro del contenedor PALBE no conoce su
   dominio publico, y una absoluta convertiria la franja del recibidor en un
   redirector abierto.

Y una trampa que merece leerse antes de tocar esto: `get_active_context()` NO
recibe el usuario. Lo lee de un ContextVar, y `_active_ctx_key()` devuelve 0
si nadie lo ha fijado -- asi que olvidar `set_current_user_id()` no da ningun
error: da una consulta valida sobre `user_id = 0` y un contexto vacio para
todo el mundo. En las peticiones normales lo fija `_AuthMiddleware`; aqui, al
ser ruta publica, hay que fijarlo a mano.
"""
from __future__ import annotations

import logging
import os
import secrets

from fastapi import Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

RUTA = "/api/plataforma/contexto"
CABECERA = "X-SGE-Plataforma"

# Cuantos companeros como maximo. El criterio de "reciente" NO vive aqui: el
# recibidor aplica su propia ventana, para que el umbral este en un solo sitio.
MAX_COMPANEROS = 3

# El numero de paso NO es el de la ruta, y esto costo un fallo: copia de la
# tabla de `_step_links` en app_palbe_4.py, que es la fuente de verdad (la usa
# la propia navegacion de PALBE). El paso 2 vive en /step/ipmvp, el 3 en
# /step/exploracion y el 4 en /step/3; mandar a /step/2 llevaba a "Paso 2 --
# Analisis Claude" y /step/3 a "Depuracion", que es el paso 4.
#
# Esta repetida y no importada porque `_step_links` vive en el monolito, que
# importa este modulo, y devuelve HTML. Si cambia alli, tiene que cambiar
# aqui.
RUTA_DEL_PASO = {
    1: "/step/1",
    2: "/step/ipmvp",
    3: "/step/exploracion",
    4: "/step/3",
    5: "/step/5",
    6: "/step/6",
}
PASO_MAX = max(RUTA_DEL_PASO)

# Mismo cuerpo para "secreto ausente" y "secreto equivocado": que no se pueda
# distinguir evita ofrecer un oraculo de identidades.
_NO_AUTORIZADO = JSONResponse({"error": "no autorizado"}, status_code=401)
_SIN_CONTEXTO = {"herramienta": "palbe", "contexto": None}


def _token_configurado() -> str:
    return os.environ.get("PALBE_CONTEXTO_TOKEN", "").strip()


def _url_del_paso(paso) -> str:
    """La ruta de reanudacion, siempre una que existe de verdad.

    Un paso que no es del asistente -- `/step/4` (Estadisticas) escribe
    step=0, y manana puede aparecer otro numero -- lleva a la portada de
    PALBE, que ya ensena el contexto activo. Nunca a una ruta inventada."""
    try:
        return RUTA_DEL_PASO.get(int(paso), "/")
    except (TypeError, ValueError):
        return "/"


def _puede_ver(palbe_db, user_id: int, project_id: int) -> bool:
    """La misma regla que `_can_access_project` de app_palbe_4: los admin ven
    todos los proyectos, el resto los suyos.

    Esta repetida aqui y no importada porque esa funcion vive en el monolito,
    que importa este modulo -- traerla seria un import circular. Si la regla
    cambia alli, tiene que cambiar aqui.

    Hace falta porque el contexto activo es una FOTO VIEJA: puede apuntar a un
    proyecto del que a esa persona ya la han sacado, y sin comprobarlo una
    fila rancia filtraria quien anda ahora en un proyecto que ya no es suyo."""
    usuario = palbe_db.get_user_by_id(user_id)
    if usuario is None:
        return False
    if getattr(usuario, "role", "") == "admin":
        return True
    return project_id in {p.id for p in palbe_db.get_user_projects(user_id)}


def _leer(palbe_db, sub: str) -> dict:
    """Todo el trabajo, en un hilo: son lecturas de SQLite.

    Va en run_in_threadpool porque bloquear el bucle de eventos mientras un
    entrenamiento tiene la base ocupada no es teorico en PALBE."""
    user_id = palbe_db.get_sso_mapping(sub)
    if user_id is None:
        return _SIN_CONTEXTO

    # Sin esto, get_active_context() leeria el contexto del usuario 0. Ver la
    # trampa en la cabecera del modulo.
    palbe_db.set_current_user_id(user_id)
    ctx = palbe_db.get_active_context()
    if not ctx.project_id or not ctx.project_name:
        return _SIN_CONTEXTO
    if not _puede_ver(palbe_db, user_id, ctx.project_id):
        return _SIN_CONTEXTO

    partes = []
    if ctx.session_name:
        partes.append(ctx.session_name)
    if ctx.iteration_name:
        partes.append(ctx.iteration_name)
    # Solo si es un paso del asistente: "paso 0 de 6" no significa nada para
    # quien lo lee, y step=0 es un valor real que escribe /step/4.
    if ctx.step in RUTA_DEL_PASO:
        partes.append(f"paso {ctx.step} de {PASO_MAX}")

    return {
        "herramienta": "palbe",
        "contexto": {
            "titulo": ctx.project_name,
            # Lo redacta PALBE y no el recibidor: si el recibidor tuviera que
            # saber que es una "iteracion", la plataforma pasaria a conocer el
            # interior de las herramientas.
            "detalle": " · ".join(partes),
            "url": _url_del_paso(ctx.step),
            # Cuando estuvo aqui por ultima vez. La escribe la navegacion
            # normal de PALBE (set_active_context), no esta ruta. Vacia en las
            # filas anteriores al 2026-09-18, hasta que la persona vuelva a
            # navegar -- y el contrato la admite vacia.
            "visto_en": ctx.visto_en,
            "companeros": palbe_db.list_companeros_de_proyecto(
                ctx.project_id, user_id, MAX_COMPANEROS
            ),
        },
    }


def register_routes(app, token: str | None = None) -> bool:
    """Registra la ruta de contexto. Devuelve False si no procede.

    Sin secreto no se registra NADA -- 404, no 401 -- igual que
    `register_sso_routes` con el flag apagado: las rutas no existen, que es
    mas seguro que existir y devolver un error.

    `token` se puede pasar para probar el caso vacio: la app se importa una
    sola vez por proceso de pytest, asi que su registro ya ha ocurrido y
    ningun monkeypatch posterior podria deshacerlo."""
    configurado = _token_configurado() if token is None else token.strip()
    if not configurado:
        logger.info(
            "[CONTEXTO] PALBE_CONTEXTO_TOKEN vacia: no se registra %s. "
            "La franja del recibidor no incluira a PALBE.", RUTA
        )
        return False

    import palbe_db

    @app.get(RUTA)
    async def contexto_de_usuario(request: Request, sub: str = Query(default="")):
        # El secreto se lee en cada peticion, no se captura del cierre: asi
        # rotarlo no exige reconstruir la imagen.
        esperado = _token_configurado() or configurado
        # En BYTES, no en str: secrets.compare_digest lanza TypeError con
        # cadenas no-ASCII, y Starlette decodifica las cabeceras como
        # latin-1. Como esta ruta es publica, cualquiera de la red podia
        # provocar un 500 con traza mandando un acento en el secreto -- en
        # vez del 401 indistinguible que toca.
        recibido = request.headers.get(CABECERA, "").encode("utf-8", "replace")
        if not secrets.compare_digest(recibido, esperado.encode("utf-8")):
            return _NO_AUTORIZADO

        if not sub.strip():
            return JSONResponse({"error": "falta sub"}, status_code=400)

        return JSONResponse(await run_in_threadpool(_leer, palbe_db, sub.strip()))

    return True


# --- Quien esta trabajando ahora (incremento 2) --------------------------

RUTA_ACTIVOS = "/api/plataforma/activos"


def _leer_activos(palbe_db, vistos: dict) -> dict:
    """Quien ha hecho algo en PALBE, con su sub de Keycloak.

    PALBE ya sabia esto antes de que existiera el bloque del recibidor: es
    `_USER_LAST_SEEN`, el mismo dato que alimenta su "Online ahora". Esta ruta
    lo LEE; no lo escribe, y eso importa mas aqui que en el contexto -- si
    pasara por el middleware, preguntar quien esta dentro pondria a todo el
    mundo dentro, y la ruta se falsearia a si misma.

    Quien no tiene fila en `sso_user_mapping` se omite: sin sub el recibidor
    no puede agrupar, y no se le va a inventar un identificador.

    No se filtra por antiguedad aqui: se mandan las fechas en crudo y la
    ventana la aplica el recibidor, para que el criterio viva en un solo
    sitio."""
    identidades = palbe_db.subs_por_usuario()
    activos = []
    for user_id, visto_en in list(vistos.items()):
        quien = identidades.get(int(user_id))
        if quien is None:
            continue
        activos.append({"sub": quien["sub"], "nombre": quien["nombre"],
                        "visto_en": visto_en.isoformat()})
    return {"herramienta": "palbe", "activos": activos}


def register_activos(app, vistos: dict, token: str | None = None) -> bool:
    """Registra la ruta de activos. Mismas reglas que la de contexto.

    `vistos` se recibe por argumento y no se importa: vive en app_palbe_4, que
    es quien importa este modulo -- traerlo de vuelta seria un import
    circular. Asi el modulo sigue sin saber nada del monolito."""
    configurado = _token_configurado() if token is None else token.strip()
    if not configurado:
        logger.info("[CONTEXTO] sin secreto: no se registra %s", RUTA_ACTIVOS)
        return False

    import palbe_db

    @app.get(RUTA_ACTIVOS)
    async def activos_de_plataforma(request: Request):
        esperado = _token_configurado() or configurado
        recibido = request.headers.get(CABECERA, "").encode("utf-8", "replace")
        if not secrets.compare_digest(recibido, esperado.encode("utf-8")):
            return _NO_AUTORIZADO
        return JSONResponse(await run_in_threadpool(_leer_activos, palbe_db, vistos))

    return True
