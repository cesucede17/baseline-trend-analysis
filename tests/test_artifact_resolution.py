"""
Resolucion de artefactos con rutas absolutas rotas (Fase 0, hallazgo):
iterations.model_path apunta a `Desktop\\JARVIS\\PALBE_DEMO\\outputs\\...`,
una ruta que ya no existe. El Paso 4 anade `_resolve_artifact(path)`,
que si la ruta guardada no existe reintenta `OUTPUTS_DIR / Path(path).name`
-- sin ningun UPDATE en la base de datos.

Hoy la funcion no existe: este test documenta el comportamiento deseado
y falla a proposito hasta el Paso 4.

Usa la iteracion real id=26 (sesion 11, HTTP_Train_Test), cuyo
model_path roto es del hallazgo original de la Fase 0. El fichero real
de esa fecha ya no sobrevive en outputs/ (es de mayo de 2026), asi que
se coloca un placeholder CON EL MISMO NOMBRE en el outputs_dir aislado
de este test -- nunca en el fixture compartido.
"""
from pathlib import Path

BROKEN_ITERATION_ID = 26
BROKEN_MODEL_PATH = (
    r"C:\Users\CesarSuelaCedenilla\Desktop\JARVIS\PALBE_DEMO\outputs"
    r"\palbe_model_OLS_20260512_145859_ols.joblib"
)


def test_iteracion_26_tiene_la_ruta_rota_documentada(db_path):
    """Confirma que el fixture sigue reflejando el hallazgo original de
    la Fase 0 -- si esto falla, los datos de prueba cambiaron y el resto
    del test ya no es representativo."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT model_path FROM iterations WHERE id=?", (BROKEN_ITERATION_ID,)
    ).fetchone()
    conn.close()
    assert row is not None, f"la iteracion {BROKEN_ITERATION_ID} no existe en el fixture"
    assert row[0] == BROKEN_MODEL_PATH


def test_resolve_artifact_encuentra_el_fichero_por_nombre(db_path, outputs_dir):
    import app_palbe_4

    if not hasattr(app_palbe_4, "_resolve_artifact"):
        raise AssertionError(
            "app_palbe_4._resolve_artifact no existe todavia (se anade en el Paso 4)"
        )

    basename = Path(BROKEN_MODEL_PATH).name
    placeholder = outputs_dir / basename
    placeholder.write_bytes(b"placeholder de prueba, no un joblib real")

    resolved = app_palbe_4._resolve_artifact(BROKEN_MODEL_PATH)
    assert resolved is not None
    assert Path(resolved).name == basename
    assert Path(resolved).exists()
