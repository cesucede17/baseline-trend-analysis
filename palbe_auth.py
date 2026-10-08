"""
palbe_auth.py — Authentication helpers for PALBE v5
Uses bcrypt directly (passlib is incompatible with bcrypt>=4.x).
"""
import bcrypt
from typing import Optional
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi import HTTPException

from palbe_db import User, get_user_by_id, get_user_by_username, list_users

# ---------------------------------------------------------------------------
# Color palette — one per user, cycled by insertion order
# ---------------------------------------------------------------------------

USER_COLORS = [
    "#16a34a",  # green
    "#2563eb",  # blue
    "#dc2626",  # red
    "#d97706",  # amber
    "#7c3aed",  # violet
    "#0891b2",  # cyan
    "#be185d",  # pink
    "#65a30d",  # lime
    "#9a3412",  # orange-dark
    "#475569",  # slate
]


def next_color() -> str:
    """Return the next available color (cycles if all taken)."""
    users = list_users()
    taken = {u.color for u in users}
    for c in USER_COLORS:
        if c not in taken:
            return c
    return USER_COLORS[len(users) % len(USER_COLORS)]


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate(username: str, password: str) -> Optional[User]:
    """Return User if credentials are valid and account is active, else None."""
    user = get_user_by_username(username)
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def get_current_user(request: Request) -> Optional[User]:
    """Read user_id from session cookie and return the User object, or None.

    Fase 4c -- INTERSECCION DE ROLES. Si la sesion entro por SSO, la sesion
    lleva "sso_roles". El rol de Keycloak solo puede RESTRINGIR: si no
    incluye "admin", se rebaja el rol aunque la base de datos diga admin.
    Nunca al reves -- un claim mal configurado no puede conceder privilegios
    que la base de datos no otorga. Se aplica aqui, en un solo sitio, y las
    21 guardas `user.role != "admin"` de app_palbe_4.py lo heredan."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = get_user_by_id(int(user_id))
    if user is None:
        return None
    if not user.is_active:
        # Cierra un hueco preexistente: una cookie viva sobrevivia a la
        # desactivacion del usuario (por login propio o por SSO). Un solo
        # sitio, y las 21 guardas y el propio camino SSO lo heredan.
        return None
    sso_roles = request.session.get("sso_roles")
    if sso_roles is not None and user.role == "admin" and "admin" not in sso_roles:
        user.role = "user"
    return user


def require_auth(request: Request) -> User:
    """
    Return current user or raise a redirect to /login.
    Use as: user = require_auth(request)
    """
    user = get_current_user(request)
    if not user:
        raise _redirect_to_login(request)
    return user


def require_admin(request: Request) -> User:
    """Return current user only if admin, else raise 403."""
    user = require_auth(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Se requiere rol de administrador.")
    return user


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _redirect_to_login(request: Request) -> RedirectResponse:
    """Al mismo sitio que el middleware de app_palbe_4: con el SSO en pie,
    al SSO. No es coherencia decorativa -- este camino se recorre cuando
    get_current_user() devuelve None con la cookie viva, que es justo lo que
    pasa con un usuario DESACTIVADO, y ahi el formulario propio es un
    callejon sin salida si la cuenta nacio por SSO.

    El import va dentro: palbe_sso importa palbe_auth (next_color,
    verify_password), asi que en cabecera seria un ciclo."""
    import palbe_sso

    return RedirectResponse(
        url=palbe_sso.destino_sin_sesion(str(request.url.path)), status_code=303)
