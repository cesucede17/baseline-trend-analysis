"""
Baseline de visibilidad automatizado (Fase 5, adelantado aqui): sustituye
al procedimiento manual. Ejecuta la consulta de membresias exacta del
documento principal contra una copia del fixture y la compara con un
fichero de referencia VERSIONADO -- debe ser identico byte a byte.

Estado esperado documentado (no confundir con una regresion si aparece
de nuevo): agarcia no tiene ninguna fila en project_users (columnas de
proyecto a NULL); STX-AWS (id=4) no aparece para NINGUN usuario no-admin,
solo lo ve csuela por ser admin (eso se prueba en test_visibility.py, no
aqui: aqui solo se congela la fila cruda tal cual sale de la BD).
"""
import json
import sqlite3
from pathlib import Path

BASELINE_FILE = Path(__file__).resolve().parent / "baseline_visibility.json"

MEMBERSHIP_QUERY = """
SELECT u.id, u.username, u.role, p.id, p.name, p.is_active
FROM users u
LEFT JOIN project_users pu ON pu.user_id = u.id
LEFT JOIN projects p ON p.id = pu.project_id
ORDER BY u.id, p.id
"""

TABLE_ROW_COUNTS = [
    "users",
    "projects",
    "sessions",
    "iterations",
    "project_users",
    "audit_log",
    "active_context",
    "active_context_user",
    "iteration_params",
    "iteration_features",
]


def _snapshot(db_path) -> dict:
    conn = sqlite3.connect(db_path)
    membership_rows = conn.execute(MEMBERSHIP_QUERY).fetchall()
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLE_ROW_COUNTS}
    conn.close()
    return {
        "membership": [list(r) for r in membership_rows],
        "row_counts": counts,
    }


def test_baseline_de_visibilidad_no_ha_cambiado(db_path):
    actual = _snapshot(db_path)

    if not BASELINE_FILE.exists():
        raise AssertionError(
            f"Falta el fichero de referencia {BASELINE_FILE}. "
            f"Generarlo deliberadamente (nunca a ciegas) y comprobarlo a mano "
            f"antes de versionarlo."
        )

    expected = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    assert actual == expected, (
        "El baseline de visibilidad ha cambiado respecto al fichero de "
        "referencia versionado. Si el cambio es intencionado (p.ej. una "
        "membresia nueva legitima), regenerar baseline_visibility.json a "
        "mano, no automaticamente, y explicar por que en el commit."
    )
