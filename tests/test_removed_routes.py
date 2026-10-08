"""
Rutas que el Paso 4 elimina por completo: /remote-control (protegido
solo por accidente -- sin PALBE_REMOTE_KEY configurada, cualquiera con
la cabecera correcta entraria) y /api/login-problem (el aviso por
Gmail, con su credencial SMTP en claro y su rate limit que interfiere
con el del login).

Se inicia sesion antes de comprobar: sin sesion, la ruta no encontrada
pasaria primero por la redireccion 303 a /login del middleware, y
TestClient sigue redirecciones por defecto -- el 200 final seria la
pagina de login, no una prueba de que la ruta exista. Con sesion, un
404 solo puede significar que la ruta de verdad no existe.

Esta protege contra que las rutas reaparezcan por descuido en un merge
futuro: si alguien reintroduce el endpoint, este test empieza a fallar
inmediatamente.
"""
from conftest import ADMIN_USER, login_as


def test_remote_control_eliminado(client):
    login_as(client, ADMIN_USER)
    resp = client.post("/remote-control", json={"action": "noop"})
    assert resp.status_code == 404, (
        f"/remote-control deberia estar eliminado, pero respondio {resp.status_code}"
    )


def test_api_login_problem_eliminado(client):
    login_as(client, ADMIN_USER)
    resp = client.post("/api/login-problem", json={"name": "x", "message": "y"})
    assert resp.status_code == 404, (
        f"/api/login-problem deberia estar eliminado, pero respondio {resp.status_code}"
    )
