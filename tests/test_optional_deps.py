"""
Dependencias opcionales que degradan en silencio (Fase 0 y Fase 4a):
_CORE_AVAILABLE (palbe_core) y _kaleido_ok (palbe_ipmvp, expuesto a nivel
de modulo en el Paso 4, junto a _CORE_AVAILABLE -- antes solo era una
variable local dentro de generate_docx, sin forma de comprobarlo desde
fuera sin generar un informe completo).

Es lo que sostiene el pin plotly<7 de la Fase 4b: si una futura subida de
plotly rompe el motor v0 de kaleido, esto debe fallar aqui, no en el
informe de un cliente.
"""
import sys

import pytest


def test_core_available_es_true():
    """palbe_core.py es byte-identico en las tres copias donde existe
    (Fase 0); si esto es False, el entrenamiento esta degradado en
    silencio (el bug historico de PALBE_DEMO)."""
    import app_palbe_4

    assert app_palbe_4._CORE_AVAILABLE is True


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "kaleido 0.2.1 (el binario autocontenido de la era pre-1.0, elegido "
        "en la Fase 4b precisamente para no depender de un Chrome externo) "
        "se queda colgado indefinidamente en write_image() en Windows -- "
        "verificado y documentado en el Anexo del documento principal. En "
        "Linux (el destino real, probado en Docker) rasteriza sin problema. "
        "Este test se salta aqui adrede: el rasterizado real se valida en "
        "el contenedor (Paso 5, punto 7 del checklist end-to-end), no en "
        "este dev machine Windows."
    ),
)
def test_kaleido_rasteriza_una_figura_real():
    """Reproduce exactamente la llamada de palbe_ipmvp.py (API legacy
    pio.kaleido.scope, escrita para la era 0.2.x de kaleido)."""
    import plotly.graph_objects as go
    import plotly.io as pio

    pio.kaleido.scope.default_format = "png"
    fig = go.Figure(go.Scatter(y=[1, 2, 3]))
    png_bytes = fig.to_image(format="png", width=200, height=100)
    assert len(png_bytes) > 500, "el PNG generado es sospechosamente pequeno"


def test_kaleido_ok_expuesto_a_nivel_de_modulo():
    """_kaleido_ok se calcula al importar palbe_ipmvp (igual que
    _CORE_AVAILABLE en app_palbe_4.py): permite comprobar si el
    rasterizado esta degradado sin generar un informe completo.
    Deliberadamente NO se comprueba is True aqui: en Windows la propia
    comprobacion de kaleido cuelga (ver el test de arriba), asi que este
    test solo verifica que el ATRIBUTO existe y es un booleano -- el
    valor real (True en Linux/Docker) lo prueba el checklist end-to-end
    del Paso 5, no este test."""
    import palbe_ipmvp

    assert isinstance(palbe_ipmvp._kaleido_ok, bool)
