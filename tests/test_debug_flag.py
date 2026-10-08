"""
Fuga de tracebacks al navegador (Fase 4a). Antes se filtraba el
traceback completo en 9 sitios: el handler global de excepciones y 8
returns inline (algunos ya los habia detectado el analisis original,
otros aparecieron al hacer un grep completo de format_exc()). Ahora
todos pasan por _safe_traceback(), que solo devuelve el traceback real
si PALBE_DEBUG=1 -- y siempre lo registra en el log del servidor,
gated o no.
"""
import logging


def test_sin_palbe_debug_el_traceback_no_llega_al_cliente(monkeypatch, caplog):
    import app_palbe_4

    monkeypatch.setattr(app_palbe_4, "_PALBE_DEBUG", False)
    try:
        raise ValueError("boom de prueba, nunca deberia verse en el navegador")
    except ValueError:
        with caplog.at_level(logging.ERROR, logger="palbe.errors"):
            resultado = app_palbe_4._safe_traceback()

    assert "boom de prueba" not in resultado
    assert resultado == "Error interno. Revisa los logs del servidor para mas detalle."
    # el traceback real se sigue registrando, aunque no se muestre al cliente
    assert any("boom de prueba" in r.message for r in caplog.records)


def test_con_palbe_debug_el_traceback_completo_es_visible(monkeypatch):
    import app_palbe_4

    monkeypatch.setattr(app_palbe_4, "_PALBE_DEBUG", True)
    try:
        raise ValueError("boom de prueba con debug activado")
    except ValueError:
        resultado = app_palbe_4._safe_traceback()

    assert "boom de prueba con debug activado" in resultado
    assert "Traceback (most recent call last)" in resultado
