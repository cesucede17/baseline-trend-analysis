"""
IDOR del wizard (Fase 4a, riesgo 3): siete endpoints que no comprueban
_can_access_project. Estos tests documentan el comportamiento CORRECTO
(403 para un usuario sin acceso al proyecto) y HOY FALLAN a proposito --
el Paso 4 los cierra, y entonces estos tests deben pasar sin tocarlos.

Datos reales usados como "victima": session_id=24 e iteration_id=32
pertenecen al proyecto 22 (Vigilancia). jmorales no tiene ese proyecto
asignado (solo HTTP_Train_Test=11 e IQE=26) -- es el atacante en los
siete tests.

/iterations/{id}/discard borra los artefactos del disco (unica funcion
que lo hace, segun el comentario de app_palbe_4.py junto a la ruta): usar
la victima aqui es seguro porque cada test opera sobre la COPIA temporal
aislada de datos_prueba/, nunca sobre el fixture compartido.
"""
from conftest import login_as

ATACANTE = "jmorales"
VICTIMA_SESSION_ID = 24  # proyecto 22 (Vigilancia), jmorales no lo tiene
VICTIMA_ITERATION_ID = 32  # de la sesion 24


def test_discard_de_iteracion_ajena(client):
    login_as(client, ATACANTE)
    resp = client.post(f"/iterations/{VICTIMA_ITERATION_ID}/discard")
    assert resp.status_code == 403, (
        f"{ATACANTE} pudo descartar (borrar artefactos) de una iteracion "
        f"del proyecto Vigilancia sin tener acceso a el"
    )


def test_save_de_iteracion_ajena(client):
    login_as(client, ATACANTE)
    resp = client.post(f"/iterations/{VICTIMA_ITERATION_ID}/save")
    assert resp.status_code == 403, f"{ATACANTE} pudo guardar una iteracion ajena"


def test_new_iteration_sobre_sesion_ajena(client):
    login_as(client, ATACANTE)
    resp = client.get(f"/sessions/{VICTIMA_SESSION_ID}/new-iteration", follow_redirects=False)
    assert resp.status_code == 403, (
        f"{ATACANTE} pudo activar el contexto de una sesion ajena (proyecto Vigilancia)"
    )


def test_start_fresh_sobre_iteracion_ajena(client):
    login_as(client, ATACANTE)
    resp = client.post(f"/iterations/{VICTIMA_ITERATION_ID}/start-fresh", follow_redirects=False)
    assert resp.status_code == 403


def test_modify_sobre_iteracion_ajena(client):
    login_as(client, ATACANTE)
    resp = client.post(f"/iterations/{VICTIMA_ITERATION_ID}/modify", follow_redirects=False)
    assert resp.status_code == 403


def test_metrics_evolution_de_sesion_ajena(client):
    login_as(client, ATACANTE)
    resp = client.get(f"/api/sessions/{VICTIMA_SESSION_ID}/metrics-evolution")
    assert resp.status_code == 403, (
        f"{ATACANTE} pudo leer metricas de una sesion ajena (hoy solo comprueba 'if not user')"
    )


def test_chart_correlation_de_iteracion_ajena(client):
    login_as(client, ATACANTE)
    resp = client.get(f"/iterations/{VICTIMA_ITERATION_ID}/chart-correlation")
    assert resp.status_code == 403
