"""
/outputs se sirve sin autenticacion (Fase 0, hallazgo): esta en la lista
blanca del middleware y montado como StaticFiles, con nombres
predecibles. Estos tests documentan el comportamiento CORRECTO --
redireccion sin sesion, 403 sin permiso, descarga con sesion legitima --
y HOY FALLAN a proposito. El Paso 4 lo cierra sustituyendo el mount por
un endpoint que aplica _can_access_project.

El fichero usado existe de verdad en la muestra de datos_prueba/outputs/
(iteracion real de la sesion 29, proyecto 22 Vigilancia).
"""
from conftest import ADMIN_USER, login_as

ARCHIVO_REAL = "palbe_model_OLS_20260609_144013_ols.joblib"  # sesion 29, proyecto 22
ATACANTE_SIN_ACCESO = "jmorales"  # no tiene el proyecto 22 (Vigilancia)


def test_outputs_sin_sesion_redirige_a_login(client):
    resp = client.get(f"/outputs/{ARCHIVO_REAL}", follow_redirects=False)
    assert resp.status_code == 303, (
        "/outputs deberia exigir sesion, igual que el resto de la app "
        "(hoy esta en la lista blanca del middleware y se sirve sin login)"
    )
    assert resp.headers["location"].startswith("/login")


def test_outputs_con_sesion_sin_permiso_da_403(client):
    login_as(client, ATACANTE_SIN_ACCESO)
    resp = client.get(f"/outputs/{ARCHIVO_REAL}")
    assert resp.status_code == 403, (
        f"{ATACANTE_SIN_ACCESO} pudo descargar un artefacto del proyecto "
        f"Vigilancia sin tener acceso a el"
    )


def test_outputs_con_sesion_legitima_descarga(client):
    login_as(client, ADMIN_USER)  # admin: acceso a todos los proyectos
    resp = client.get(f"/outputs/{ARCHIVO_REAL}")
    assert resp.status_code == 200
    assert len(resp.content) > 0
