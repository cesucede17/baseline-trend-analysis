"""
Fase 4c -- capa OIDC contra Keycloak, detras del flag PALBE_SSO_ENABLED.

Vive en su propio fichero a proposito: app_palbe_4.py ya tiene 8.900 lineas,
y meter OIDC dentro haria el cambio mas dificil de revisar y de revertir.
El modulo se importa SIEMPRE (app_palbe_4.py hace `import palbe_sso`
incondicional) -- lo que depende del flag es si register_sso_routes()
registra algo. Consecuencia: authlib es dependencia dura de la imagen
aunque PALBE_SSO_ENABLED este apagado, porque este fichero lo importa en
cabecera.

El contrato con el resto de la aplicacion es minusculo: al final del baile
OIDC se escribe request.session["user_id"], exactamente la misma linea que
el login propio (app_palbe_4.py:1567). Nada mas se entera.

Desde el 2026-09-22 resolver como user_id no es un mapeo directo sino una
cascada: si el sub ya esta vinculado se usa ese mapeo tal cual, y si no,
_alta_automatica() decide con quien entrar (o si hay que negar el paso).
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

SSO_ENABLED = os.environ.get("PALBE_SSO_ENABLED", "0") == "1"

# Si register_sso_routes() llego a registrar /sso/login. NO es lo mismo que
# SSO_ENABLED: el flag puede estar encendido y la configuracion incompleta,
# y entonces la ruta no existe y mandar ahi seria un 404.
_RUTAS_REGISTRADAS = False


def _destino_seguro(candidato: str) -> str:
    """Una ruta relativa de esta aplicacion, o "/".

    Cuatro comprobaciones, cada una cierra una tecnica distinta de
    convertir el SSO en un redirector abierto con el sello de la
    plataforma. NO simplificar a "empieza por / y no por //": ese era el
    guardian viejo, y dos tecnicas conocidas lo atraviesan sin tocar esos
    dos caracteres.

    - Empieza por "/" y no por "//": "//evil.com" ya es una URL
      protocol-relative de por si -- ni falta el esquema.
    - Sin backslash: los navegadores normalizan "\\" a "/" al parsear una
      URL de esquema especial (WHATWG). "/\\evil.com" pasa el primer
      filtro (empieza por "/", el segundo caracter no es "/") pero el
      navegador lo interpreta como "//evil.com" -- fuera de la
      plataforma. Python nunca trata "\\" como separador dentro de
      urlsplit (comprobacion de mas abajo), asi que esa comprobacion NO
      lo detecta: hay que rechazarlo aqui, explicitamente.
    - Sin caracteres de control (tab, salto de linea, retorno de carro,
      cualquier ord() < 0x20): el estandar WHATWG obliga a eliminarlos al
      parsear una URL, asi que "/\t//evil.com" decodifica del query string
      con un tab literal, pasa "empieza por / y el caracter siguiente no
      es /", y el navegador lo ve como "//evil.com" tras quitar el tab.
      OJO -- urlsplit() de Python TAMBIEN elimina tabuladores/CR/LF por su
      cuenta (arreglo de seguridad de CPython, bpo-43882), pero eso no
      hace redundante esta comprobacion: tras esa limpieza interna,
      "/\t//evil.com" queda en "///evil.com" (tres barras: quitar el tab
      pega el "/" de delante con el "//" de detras), y el algoritmo de
      urlsplit lee esa tercera barra como delimitador de path y no como
      inicio de autoridad, asi que sigue devolviendo netloc="" -- por un
      motivo que no tiene nada que ver con el navegador. Comprobado en
      Python 3.14: urlsplit("///evil.com") da netloc="".
    - scheme y netloc vacios via urlsplit: red de seguridad para
      variantes que no se nos hayan ocurrido, NO sustituto de las dos
      comprobaciones de arriba. Sin ellas, urlsplit por si solo deja pasar
      tanto "/\\evil.com" como "/\t//evil.com" (netloc="" en ambos, por
      las razones ya explicadas), asi que quitar la comprobacion de
      backslash/control reabriria exactamente los dos ataques que
      motivaron este cambio.
    """
    if not candidato.startswith("/") or candidato.startswith("//"):
        return "/"
    if "\\" in candidato or any(ord(c) < 0x20 for c in candidato):
        return "/"
    partes = urlsplit(candidato)
    if partes.scheme or partes.netloc:
        return "/"
    return candidato


def destino_sin_sesion(next_path: str) -> str:
    """A donde mandar a quien llega sin sesion. Con el SSO en pie, al SSO:
    quien ya tiene sesion en la plataforma entra sin ver ninguna pantalla, y
    quien no, ve el login de Keycloak, que es el que toca. El formulario
    propio sigue existiendo escribiendo /login a mano -- es la puerta de
    rescate si Keycloak se cae."""
    ruta = _destino_seguro(next_path)
    if _RUTAS_REGISTRADAS:
        return f"/sso/login?next={ruta}"
    return f"/login?next={ruta}"


@dataclass(frozen=True)
class SSOConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def metadata_url(self) -> str:
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"


def load_config() -> Optional[SSOConfig]:
    """Devuelve la configuracion, o None si falta CUALQUIER variable.

    Todo o nada a proposito: una configuracion a medias falla a mitad del
    baile OIDC, con un error que no dice nada. Asi falla al arrancar."""
    issuer = os.environ.get("PALBE_SSO_ISSUER", "").strip()
    client_id = os.environ.get("PALBE_SSO_CLIENT_ID", "").strip()
    client_secret = os.environ.get("PALBE_SSO_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("PALBE_SSO_REDIRECT_URI", "").strip()
    if not (issuer and client_id and client_secret and redirect_uri):
        return None
    return SSOConfig(issuer, client_id, client_secret, redirect_uri)


# El rol de realm que abre PALBE. Ver la espec: el rol pasa de decorar la
# tarjeta del recibidor a ser la puerta de verdad, para que quitarselo a
# alguien en Keycloak surta efecto y no solo esconda el icono.
#
# Se comprueba en CADA acceso, tambien a quien ya esta vinculado. No es una
# puerta de alta: si solo se mirase al vincular, quitar el rol no surtiria
# efecto jamas sobre nadie que hubiera entrado alguna vez -- y desde que
# todo el mundo se vincula en su primer acceso, eso es la plantilla entera.
# La UNICA escotilla es la lista de roles vacia: ver _puerta_del_rol.
ROL_PALBE = "palbe"

# El password_hash de una cuenta nacida por SSO. NO es hash_password("") --
# eso regalaria una cuenta sin contrasena por el formulario propio a cada
# persona que se diera de alta. Es una cadena que no es un hash de bcrypt, y
# palbe_auth.verify_password devuelve False ante cualquier cosa que haga
# saltar a bcrypt. Consecuencia buscada: estas cuentas SOLO entran por SSO.
_SIN_CONTRASENA_PROPIA = "sso-sin-contrasena-propia"


def _roles_del_token(claims: dict) -> list:
    """Los roles de realm del token, normalizados a lista. Cualquier forma
    rara (sin realm_access, con realm_access vacio, con roles a None) cae en
    la misma categoria: lista vacia, que es la que tiene tratamiento
    especial."""
    return list((claims.get("realm_access") or {}).get("roles") or [])


def _puerta_del_rol(claims: dict) -> bool:
    """Si este token pasa la puerta del rol `palbe`.

    Con una escotilla, y una sola: si la lista de roles viene VACIA no se
    interpreta como "no tiene el rol". Todo usuario real lleva al menos
    default-roles-sge, asi que una lista vacia significa que el mapeador del
    cliente de Keycloak no mete realm_access en el id_token -- que es lo que
    authlib deja en token["userinfo"]. Es configuracion, no permisos, y con
    el mapeador roto la lista viene vacia para TODO el mundo: negar ahi
    dejaria fuera tambien a quien tiene que arreglarlo.

    Si la lista trae roles, en cambio, la respuesta es fiable: si `palbe` no
    esta, es que no lo tiene."""
    roles = _roles_del_token(claims)
    return not roles or ROL_PALBE in roles


def _alta_automatica(claims: dict, sub: str) -> Optional[int]:
    """Con quien entrar cuando el sub NO esta vinculado todavia, o None si
    hay que negar el paso. Registra SIEMPRE el motivo: los caminos de
    rechazo ensenan la misma pagina a proposito, asi que el registro es el
    unico sitio donde se distinguen."""
    import palbe_db

    roles = _roles_del_token(claims)
    username = (claims.get("preferred_username") or "").strip()

    if not roles:
        # NO es "no tiene el rol": todo usuario real lleva al menos
        # default-roles-sge. Una lista vacia significa que el cliente de
        # Keycloak no mete realm_access en el id_token -- que es lo que
        # authlib deja en token["userinfo"]. El mapeador por defecto del
        # realm solo pone access.token.claim=true.
        palbe_db.log_action(None, "sso_roles_ausentes", "user", None,
                            {"sub": sub, "username": username})
        return None

    if ROL_PALBE not in roles:
        palbe_db.log_action(None, "sso_denegado_sin_rol", "user", None,
                            {"sub": sub, "username": username})
        return None

    if not username:
        # No se inventa un nombre. Un usuario llamado "None" es peor que un 403.
        palbe_db.log_action(None, "sso_sin_nombre_de_usuario", "user", None,
                            {"sub": sub})
        return None

    email = (claims.get("email") or "").strip()
    # Ignorando mayusculas: si en PALBE esta "ILasierra" y Keycloak manda
    # "ilasierra", una comparacion byte a byte no lo encuentra, se crea una
    # ficha nueva y la persona acaba con DOS usuarios y sus proyectos en el
    # que ya no usa -- justo lo que este cruce existe para evitar, y
    # silencioso, porque el registro diria "sso_autoalta".
    coincidencias = palbe_db.buscar_usuarios_por_nombre_sin_caja(username)
    if len(coincidencias) > 1:
        # Dos fichas distintas que solo se diferencian en la caja. No hay
        # eleccion correcta: quedarse con la primera es repartir proyectos a
        # cara o cruz. Es una colision, como la de dos subs con el mismo
        # nombre, y se trata igual: no se toca nada.
        palbe_db.log_action(None, "sso_colision_nombre", "user", None,
                            {"sub": sub, "username": username,
                             "motivo": "varios usuarios coinciden ignorando mayusculas",
                             "candidatos": [u.username for u in coincidencias]})
        return None
    existente = coincidencias[0] if coincidencias else None

    if existente is not None:
        if not existente.is_active:
            # El alta automatica NO resucita a nadie. Se niega sin escribir
            # nada: ni vinculo, ni fila.
            palbe_db.log_action(None, "sso_login_inactive", "user", existente.id,
                                {"sub": sub, "username": username})
            return None

        sub_previo = palbe_db.get_sso_sub_de_usuario(existente.id)
        if sub_previo is not None and sub_previo != sub:
            # Ese nombre ya es de otra identidad de Keycloak. No es un alta:
            # es una colision, y heredar la ficha ajena seria darle a alguien
            # los proyectos de otro.
            palbe_db.log_action(None, "sso_colision_nombre", "user", existente.id,
                                {"sub": sub, "username": username,
                                 "sub_ya_vinculado": sub_previo})
            return None

        if sub_previo != sub:
            # Solo hace falta ESCRIBIR el vinculo cuando todavia no existe.
            # Si sub_previo == sub, es que otra pestana de esta misma
            # persona gano la carrera del primer acceso mientras esta
            # llegaba hasta aqui, y ya lo dejo escrito con este mismo sub
            # -- no es una colision, es la misma identidad.
            palbe_db.create_sso_mapping(sub, existente.id,
                                        keycloak_username=username,
                                        keycloak_email=email)
        palbe_db.log_action(existente.id, "sso_autovinculo", "user", existente.id,
                            {"sub": sub, "username": username})
        return existente.id

    import palbe_auth

    nuevo = palbe_db.create_user(
        username=username,
        password_hash=_SIN_CONTRASENA_PROPIA,
        role="user",          # NUNCA admin: un claim no concede privilegios
        color=palbe_auth.next_color(),
        email=email,
    )
    palbe_db.create_sso_mapping(sub, nuevo.id,
                                keycloak_username=username,
                                keycloak_email=email)
    palbe_db.log_action(nuevo.id, "sso_autoalta", "user", nuevo.id,
                        {"sub": sub, "username": username})
    return nuevo.id


def _pagina_de_rechazo() -> str:
    """La pagina que ven TODOS los rechazos, sea cual sea el motivo.

    Que sea la misma para todos es una decision de la espec y no se toca:
    distinguir un fallo tecnico de una cuenta rechazada le daria pistas a
    quien no debe. Lo que si tiene que hacer el texto es no mentir. El
    anterior decia "contacta con el administrador para que la vincule" y
    "no se crea ninguna cuenta automaticamente": desde que hay alta
    automatica la segunda frase es falsa, y la primera manda a pedir algo
    que ya no existe.

    Asi que: ninguna causa (no se puede saber cual es sin ensenarla), ningun
    remedio inventado, y lo unico util que el usuario puede aportar -- la
    hora, que es como se encuentra su entrada en audit_log, el unico sitio
    donde los rechazos se distinguen.

    Y sin enlace. El unico que habia iba a /login, el formulario propio: un
    bucle para quien acaba de ser rechazado, e imposible de satisfacer si su
    cuenta nacio por SSO (su password_hash es un centinela). Volver a
    intentarlo por /sso/login repetiria el mismo rechazo. El formulario
    propio sigue existiendo escribiendolo a mano, que es su papel de puerta
    de rescate."""
    ahora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>No se ha podido entrar</title></head>
<body style="font-family:system-ui;max-width:34rem;margin:4rem auto;line-height:1.6">
<h1>No se ha podido entrar en PALBE</h1>
<p>Tu identidad se ha verificado, pero PALBE no ha podido darte acceso.</p>
<p>Si crees que es un error, avisa al administrador y dile la hora exacta
que aparece aqui abajo: con ella puede encontrar en el registro que ha
pasado.</p>
<p><strong>Hora:</strong> {ahora}</p>
</body></html>"""


def resolver_sesion_sso(request, claims: dict, id_token: str = ""):
    """Todo lo que ocurre DESPUES de obtener el token. Separado del baile
    OIDC para poder probarlo sin red ni Keycloak: recibe los claims ya
    obtenidos y decide.

    Devuelve la respuesta a enviar: 303 a "/" con la sesion escrita, o 403
    con la pagina de "no vinculada" si hay que negar el paso. Si el sub ya
    esta vinculado se usa ese mapeo tal cual (el camino directo, sin pasar
    por ninguna puerta). Si no, quien decide es _alta_automatica(), que
    resuelve una cascada de tres pasos mas: el rol palbe en el token, el
    cruce por nombre de usuario con una cuenta que ya existe, y -- solo si
    ninguno de los dos aplica -- el alta de un usuario nuevo. La garantia
    de la Fase 4c (un sub sin mapear NO se auto-provisiona) no desaparecio,
    se estrecho: sin el rol palbe en el token, sigue sin crearse ni
    vincularse nada. El contrato con el resto de la aplicacion sigue
    siendo el mismo de siempre: escribir request.session["user_id"]."""
    import palbe_db

    sub = claims.get("sub", "")
    if not sub:
        palbe_db.log_action(None, "sso_login_failed", "user", None,
                            {"motivo": "token sin sub"})
        return HTMLResponse(_pagina_de_rechazo(), status_code=403)

    user_id = palbe_db.get_sso_mapping(sub)
    if user_id is not None:
        # El vinculo NO es un salvoconducto vitalicio: el rol se comprueba
        # tambien aqui, en cada acceso. Si solo se mirase al vincular, a
        # quien le quitaran el rol `palbe` en Keycloak seguiria entrando
        # para siempre -- y desde que todo el mundo se vincula en su primer
        # acceso, ese "para siempre" es la plantilla entera. La escotilla
        # (lista de roles vacia = mapeador roto, no falta de permisos) vive
        # dentro de _puerta_del_rol y es la que conserva la red del
        # despliegue.
        if not _puerta_del_rol(claims):
            # Mismo nombre de accion que el rechazo del alta, a proposito:
            # el motivo es el mismo y el runbook consulta esa lista. Lo que
            # distingue una revocacion de un alta rechazada es el
            # resource_id, que aqui SI apunta al usuario ya vinculado.
            palbe_db.log_action(None, "sso_denegado_sin_rol", "user", user_id,
                                {"sub": sub,
                                 "username": claims.get("preferred_username") or "",
                                 "vinculado": True})
            return HTMLResponse(_pagina_de_rechazo(), status_code=403)
    if user_id is None:
        # Ya no se niega en seco. Se intenta el alta automatica, que tiene su
        # propia puerta (el rol palbe) y registra el motivo de cada rechazo.
        try:
            user_id = _alta_automatica(claims, sub)
        except sqlite3.IntegrityError:
            # Dos pestanas del mismo primer acceso. La que pierde la carrera
            # vuelve a leer: la otra ya dejo el vinculo escrito, y es el
            # MISMO sub, asi que es la misma persona. Sin esto, estrenar
            # cuenta con dos pestanas abiertas da un 500.
            user_id = palbe_db.get_sso_mapping(sub)
        if user_id is None:
            # _alta_automatica ya distingue "otra identidad" (sub_previo !=
            # sub, colision de verdad) de "soy yo mismo, la otra pestana
            # gano la carrera" (sub_previo == sub, devuelve el id sin
            # negarlo). Asi que esta relectura ya NO cubre esa carrera --
            # se resuelve dentro de la cascada, sin pasar por aqui. Se deja
            # como red de seguridad ante otros entrelazados no previstos:
            # si por lo que sea el vinculo de este mismo sub ya existe
            # cuando llegamos aqui, se usa; si no existe, el None de arriba
            # era un rechazo de verdad y se mantiene.
            user_id = palbe_db.get_sso_mapping(sub)
        if user_id is None:
            return HTMLResponse(_pagina_de_rechazo(), status_code=403)

    # El nombre de persona que manda Keycloak, para que la plataforma llame
    # igual a cada uno en todas partes. PALBE solo guardaba el USUARIO, asi
    # que en el bloque "Trabajando ahora" salia "cejemplo" donde el recibidor
    # decia "Carlos Ejemplo Pérez".
    #
    # Aqui y no al vincular: vincular lo hace una persona a mano una sola vez,
    # asi que la columna se quedaria vacia para todos los que ya estaban.
    palbe_db.recordar_nombre_keycloak(
        sub, claims.get("name") or claims.get("preferred_username") or "")

    user = palbe_db.get_user_by_id(user_id)
    if user is None or not user.is_active:
        # Misma respuesta que "no vinculada", a proposito: no se dan pistas
        # sobre si la cuenta existe. El login propio ya rechaza a un usuario
        # desactivado (palbe_auth.authenticate) -- el SSO no puede conceder
        # lo que la base de datos niega, aunque Keycloak tarde en enterarse
        # de que se desactivo.
        palbe_db.log_action(None, "sso_login_inactive", "user", user_id,
                            {"sub": sub})
        return HTMLResponse(_pagina_de_rechazo(), status_code=403)

    # Antes del clear(): es lo unico que hay que salvar de la sesion vieja.
    destino = _destino_seguro(request.session.get("sso_next") or "/")
    # Se limpia la sesion antes de escribir nada: cierra cualquier residuo
    # de una sesion anterior (fijacion de sesion, o un login propio previo
    # sin cerrar sesion cuyo sso_roles sobreviviria al nuevo user_id).
    request.session.clear()
    # La MISMA linea que el login propio. Todo lo demas de la aplicacion
    # lee de aqui y no se entera de por donde entro el usuario.
    request.session["user_id"] = user_id
    request.session["sso_roles"] = list(
        (claims.get("realm_access") or {}).get("roles") or []
    )
    request.session["sso_id_token"] = id_token
    palbe_db.log_action(user_id, "login_sso", "user", user_id,
                        {"sub": sub})
    return RedirectResponse(destino, status_code=303)


def register_sso_routes(app) -> bool:
    """Registra /sso/login y /sso/callback. Devuelve False si no procede.

    Con el flag apagado o sin configuracion completa no se registra NADA:
    las rutas simplemente no existen, que es mas seguro que existir y
    devolver un error."""
    if not SSO_ENABLED:
        return False
    cfg = load_config()
    if cfg is None:
        return False

    import palbe_db

    oauth = OAuth()
    oauth.register(
        name="keycloak",
        server_metadata_url=cfg.metadata_url,
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        client_kwargs={"scope": "openid profile email"},
    )

    @app.get("/sso/login")
    async def sso_login(request: Request):
        destino = request.query_params.get("next", "")
        if destino:
            # En la sesion y no en el state de OIDC: authlib es el dueno del
            # state y no conviene disputarselo. Se recupera en el callback.
            request.session["sso_next"] = _destino_seguro(destino)
        return await oauth.keycloak.authorize_redirect(request, cfg.redirect_uri)

    @app.get("/sso/callback")
    async def sso_callback(request: Request):
        try:
            token = await oauth.keycloak.authorize_access_token(request)
        except Exception as exc:
            # Keycloak caido, code caducado, state que no cuadra... Estos
            # son justo los caminos que interesaria auditar -- un ataque de
            # state o un code reutilizado no dejan otra huella. Se devuelve
            # la misma pagina que "no vinculada": que el usuario no
            # distinga un fallo tecnico de una cuenta no vinculada es
            # deseable, no le da pistas a nadie. NUNCA se registra el token
            # ni el code, solo el tipo de excepcion y su mensaje. Truncado a
            # 500 caracteres: es un mensaje de excepcion de una libreria de
            # terceros yendo a una tabla persistente, sin cota no es de fiar.
            palbe_db.log_action(None, "sso_login_error", "user", None,
                                {"tipo": type(exc).__name__, "mensaje": str(exc)[:500]})
            return HTMLResponse(_pagina_de_rechazo(), status_code=403)

        claims = token.get("userinfo") or {}
        return resolver_sesion_sso(request, claims, id_token=token.get("id_token", ""))

    global _RUTAS_REGISTRADAS
    _RUTAS_REGISTRADAS = True
    return True


def url_de_cierre(id_token: str) -> str | None:
    """La URL de cierre de sesion de Keycloak, o None si el SSO no esta
    configurado.

    Cierre UNICO: borrar solo la cookie de PALBE dejaria viva la sesion de
    Keycloak, y entonces volver a entrar en cualquier herramienta pasaria de
    largo sin pedir nada. En un equipo compartido eso sorprende.

    client_id SIEMPRE va en los parametros: Keycloak exige id_token_hint O
    client_id para validar post_logout_redirect_uri contra las URIs
    registradas (post.logout.redirect.uris). Con id_token_hint presente es
    redundante e inofensivo; sin el -- sesion SSO cuyo token no traia
    id_token -- es lo que evita un 400 en crudo en vez de la redireccion.
    Mismo defecto ya encontrado y corregido para el cliente dashboard
    (platform/portal/backend/sso.py)."""
    from urllib.parse import urlencode

    cfg = load_config()
    if cfg is None:
        return None
    destino = os.environ.get("PALBE_SSO_POST_LOGOUT_URI", "").strip()
    params = {"client_id": cfg.client_id}
    if destino:
        params["post_logout_redirect_uri"] = destino
    if id_token:
        params["id_token_hint"] = id_token
    base = f"{cfg.issuer.rstrip('/')}/protocol/openid-connect/logout"
    return f"{base}?{urlencode(params)}" if params else base
