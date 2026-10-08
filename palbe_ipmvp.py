"""
palbe_ipmvp.py — IPMVP Preliminary Analysis Engine for PALBE v4
Covers IPMVP points 1-6: data quality, descriptive stats, exploratory graphics,
independent variable identification, correlations, and outlier detection.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import scipy.stats as scipy_stats

try:
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

# Disponibilidad de kaleido para rasterizar graficas Plotly a PNG en el
# informe Word. Expuesto a nivel de modulo (igual que _CORE_AVAILABLE en
# app_palbe_4.py) para poder comprobarlo desde /admin sin generar un
# informe completo. Antes esto era una variable LOCAL dentro de
# generate_docx, con el fallo tragado en silencio: un informe podia salir
# con el apartado de graficas vacio y ningun error en ningun sitio.
_kaleido_ok = False
_kaleido_error: Optional[str] = None
try:
    import plotly.io as _pio
    _pio.kaleido.scope.default_format = "png"
    _kaleido_ok = True
except Exception as _kaleido_exc:
    _kaleido_error = str(_kaleido_exc)
    import logging as _logging
    _logging.getLogger("palbe").error(
        "kaleido no disponible, el informe IPMVP saldra sin graficas: %s", _kaleido_error
    )


# ---------------------------------------------------------------------------
# Config & Report dataclasses
# ---------------------------------------------------------------------------

@dataclass
class IPMVPConfig:
    proyecto: str = ""
    cliente_sitio: str = ""
    contexto_operacional: str = ""
    periodo_esperado: str = ""
    frecuencia_esperada: str = "desconocida"
    variable_consumo: str = ""
    unidades_conocidas: Dict[str, str] = field(default_factory=dict)
    criterio_outliers: str = "combinado"
    nivel_detalle: str = "tecnico"
    option: str = "C"
    note: str = ""


@dataclass
class IPMVPReport:
    config: IPMVPConfig
    fecha_analisis: str
    periodo_detectado: str
    frecuencia_detectada: str
    estado_dataset: str          # "apto" | "parcialmente_apto" | "no_apto"
    n_filas_original: int
    n_filas_mantenidas: int
    n_filas_excluidas: int
    n_filas_pendientes: int
    # Secciones de texto/HTML (secciones 1-12)
    sections: List[Dict[str, str]]          # [{id, title, html}]
    # Tablas (9 tablas obligatorias)
    tables: List[Dict[str, Any]]            # [{id, title, columns, rows}]
    # Gráficas Plotly como HTML parcial
    charts: List[Dict[str, str]]            # [{id, title, html, interpretation, limitations}]
    # Validación humana
    human_validation: List[Dict[str, str]]  # [{item, reason, columns, action}]
    limitations: List[str]
    conclusion: str
    parametros_inferidos: List[Dict[str, str]]  # [{parametro, valor, evidencia, confianza}]
    # Justification data for proper Word table rendering (set by app layer)
    justification_excluded: List[Dict] = field(default_factory=list)  # [{idx, reason}]
    justification_pending: List[Dict] = field(default_factory=list)   # [{idx, reason}]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_ipmvp(
    df: pd.DataFrame,
    config: IPMVPConfig,
    y_col: str,
    x_cols: List[str],
    date_col: Optional[str] = None,
    api_key: Optional[str] = None,
) -> IPMVPReport:
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
    df = df.copy()
    df_original_len = len(df)

    # Resolve variable_consumo from config or y_col
    if not config.variable_consumo:
        config.variable_consumo = y_col

    # --- A. Inferencia de parámetros ---
    parametros_inferidos, periodo_detectado, frecuencia_detectada = _infer_params(df, config, date_col)

    # --- B. Calidad de datos ---
    quality_issues, traceability = _quality_analysis(df, y_col, x_cols, date_col, df_original_len)

    # --- C. Estadística descriptiva ---
    all_cols = [y_col] + [c for c in x_cols if c in df.columns]
    df_num = df[all_cols].apply(pd.to_numeric, errors="coerce")
    summary_stats = _build_summary(df_num)

    # --- D. Gráficas ---
    charts = _build_charts(df, df_num, y_col, x_cols, date_col)

    # --- E + F. Variables candidatas + correlaciones ---
    var_table, corr_table = _variable_analysis(df, df_num, y_col, x_cols)

    # --- G. Outliers ---
    outlier_table, outlier_rows, human_val = _outlier_analysis(df, df_num, y_col, date_col, config.criterio_outliers)

    # --- H. Narrativa Claude (opcional) ---
    narrativa_calidad = _claude_narrative(df, config, y_col, x_cols, date_col, quality_issues, summary_stats, api_key)

    # --- I. Estado del dataset ---
    estado = _evaluate_dataset_state(quality_issues, df_original_len)
    n_excluidas = traceability.get("excluidas", 0)
    n_pendientes = traceability.get("pendientes", len(outlier_rows))
    n_mantenidas = df_original_len - n_excluidas - n_pendientes

    # Limitaciones
    limitations = _collect_limitations(df, y_col, x_cols, date_col, charts)

    # Conclusión
    conclusion = _build_conclusion(estado, df_original_len, n_mantenidas, n_excluidas, n_pendientes,
                                    var_table, frecuencia_detectada)

    # Ensamblar tablas
    tables = _assemble_tables(df, df_num, y_col, x_cols, date_col, quality_issues,
                               summary_stats, charts, var_table, corr_table,
                               outlier_table, traceability, n_excluidas, n_pendientes, df_original_len)

    # Ensamblar secciones HTML
    sections = _assemble_sections(df, config, y_col, x_cols, date_col,
                                   narrativa_calidad, summary_stats, frecuencia_detectada,
                                   periodo_detectado, estado, parametros_inferidos)

    return IPMVPReport(
        config=config,
        fecha_analisis=fecha,
        periodo_detectado=periodo_detectado,
        frecuencia_detectada=frecuencia_detectada,
        estado_dataset=estado,
        n_filas_original=df_original_len,
        n_filas_mantenidas=max(0, n_mantenidas),
        n_filas_excluidas=n_excluidas,
        n_filas_pendientes=n_pendientes,
        sections=sections,
        tables=tables,
        charts=charts,
        human_validation=human_val,
        limitations=limitations,
        conclusion=conclusion,
        parametros_inferidos=parametros_inferidos,
    )


# ---------------------------------------------------------------------------
# A. Inferencia de parámetros
# ---------------------------------------------------------------------------

def _infer_params(df: pd.DataFrame, config: IPMVPConfig, date_col: Optional[str]
                  ) -> Tuple[List[Dict], str, str]:
    inferred = []
    periodo = "No detectado"
    frecuencia = config.frecuencia_esperada if config.frecuencia_esperada != "desconocida" else "desconocida"

    if date_col and date_col in df.columns:
        try:
            dates = pd.to_datetime(df[date_col], errors="coerce").dropna()
            if len(dates) >= 2:
                dmin = dates.min().strftime("%Y-%m-%d")
                dmax = dates.max().strftime("%Y-%m-%d")
                periodo = f"{dmin} → {dmax}"
                delta = (dates.max() - dates.min()).days
                n = len(dates)
                avg_gap = delta / (n - 1) if n > 1 else 0
                if avg_gap <= 1.1:
                    frecuencia = "diaria"
                elif avg_gap <= 1.5:
                    frecuencia = "diaria"
                elif avg_gap <= 7.5:
                    frecuencia = "semanal"
                elif avg_gap <= 32:
                    frecuencia = "mensual"
                else:
                    frecuencia = "irregular"
                inferred.append({"parametro": "[PERIODO_ESPERADO]", "valor": periodo,
                                  "evidencia": f"Columna {date_col}: min={dmin}, max={dmax}",
                                  "confianza": "alto"})
                inferred.append({"parametro": "[FRECUENCIA_ESPERADA]", "valor": frecuencia,
                                  "evidencia": f"Gap promedio entre registros: {avg_gap:.1f} días",
                                  "confianza": "alto" if avg_gap <= 32 else "medio"})
        except Exception:
            pass

    if not config.proyecto:
        inferred.append({"parametro": "[PROYECTO]", "valor": "No especificado",
                          "evidencia": "No proporcionado por el usuario",
                          "confianza": "bajo"})
    if not config.cliente_sitio:
        inferred.append({"parametro": "[CLIENTE_SITIO]", "valor": "No especificado",
                          "evidencia": "No proporcionado por el usuario",
                          "confianza": "bajo"})
    if not config.contexto_operacional:
        inferred.append({"parametro": "[CONTEXTO_OPERACIONAL]", "valor": "No especificado",
                          "evidencia": "No proporcionado por el usuario",
                          "confianza": "bajo"})

    return inferred, periodo, frecuencia


# ---------------------------------------------------------------------------
# B. Calidad de datos
# ---------------------------------------------------------------------------

def _quality_analysis(df: pd.DataFrame, y_col: str, x_cols: List[str],
                       date_col: Optional[str], n_orig: int) -> Tuple[List[Dict], Dict]:
    issues = []
    n = len(df)

    # Nulos por columna
    for col in df.columns:
        n_null = int(df[col].isnull().sum())
        if n_null > 0:
            pct = round(n_null / n * 100, 1)
            sev = "alta" if pct > 20 else ("media" if pct > 5 else "baja")
            issues.append({
                "problema": "Valores nulos",
                "variable": col,
                "n_registros": n_null,
                "pct": pct,
                "severidad": sev,
                "accion": "Imputar o excluir filas" if sev != "baja" else "Revisar",
            })

    # Duplicados exactos
    n_dup = int(df.duplicated().sum())
    if n_dup > 0:
        issues.append({
            "problema": "Filas duplicadas",
            "variable": "Todas",
            "n_registros": n_dup,
            "pct": round(n_dup / n * 100, 1),
            "severidad": "media",
            "accion": "Eliminar duplicados antes del modelado",
        })

    # Ceros en Y
    if y_col in df.columns:
        n_zeros = int((pd.to_numeric(df[y_col], errors="coerce") == 0).sum())
        if n_zeros > 0:
            pct_z = round(n_zeros / n * 100, 1)
            issues.append({
                "problema": "Ceros en variable objetivo",
                "variable": y_col,
                "n_registros": n_zeros,
                "pct": pct_z,
                "severidad": "alta" if pct_z > 5 else "media",
                "accion": "Verificar si representan parada o error de medición",
            })

    # Continuidad temporal
    if date_col and date_col in df.columns:
        try:
            dates = pd.to_datetime(df[date_col], errors="coerce").dropna().sort_values()
            gaps = dates.diff().dropna()
            if len(gaps) > 1:
                expected_gap = gaps.median()
                big_gaps = gaps[gaps > expected_gap * 2]
                if len(big_gaps) > 0:
                    issues.append({
                        "problema": "Huecos temporales",
                        "variable": date_col,
                        "n_registros": len(big_gaps),
                        "pct": round(len(big_gaps) / len(gaps) * 100, 1),
                        "severidad": "media",
                        "accion": "Verificar si corresponden a paradas o fallos de medición",
                    })
            dup_dates = int(dates.duplicated().sum())
            if dup_dates > 0:
                issues.append({
                    "problema": "Timestamps duplicados",
                    "variable": date_col,
                    "n_registros": dup_dates,
                    "pct": round(dup_dates / n * 100, 1),
                    "severidad": "alta",
                    "accion": "Agregar o eliminar timestamps duplicados",
                })
        except Exception:
            pass

    traceability = {
        "originales": n_orig,
        "excluidas": n_dup,
        "pendientes": 0,
        "mantenidas": n_orig - n_dup,
    }
    return issues, traceability


# ---------------------------------------------------------------------------
# C. Estadística descriptiva
# ---------------------------------------------------------------------------

def _build_summary(df_num: pd.DataFrame) -> Dict[str, Dict]:
    summary = {}
    for col in df_num.columns:
        s = df_num[col].dropna()
        if len(s) == 0:
            continue
        q1 = float(s.quantile(0.25))
        q3 = float(s.quantile(0.75))
        std = float(s.std())
        mean = float(s.mean())
        cv = round(std / mean * 100, 2) if mean != 0 else None
        summary[col] = {
            "n": int(s.count()),
            "mean": round(mean, 4),
            "median": round(float(s.median()), 4),
            "min": round(float(s.min()), 4),
            "p25": round(q1, 4),
            "p75": round(q3, 4),
            "max": round(float(s.max()), 4),
            "std": round(std, 4),
            "cv": cv,
            "iqr": round(q3 - q1, 4),
        }
    return summary


# ---------------------------------------------------------------------------
# D. Gráficas
# ---------------------------------------------------------------------------

def _build_charts(df: pd.DataFrame, df_num: pd.DataFrame, y_col: str,
                  x_cols: List[str], date_col: Optional[str]) -> List[Dict]:
    charts = []

    # 1. Serie temporal
    if date_col and date_col in df.columns and y_col in df.columns:
        charts.append(_chart_serie_temporal(df, y_col, date_col))
    else:
        charts.append({
            "id": "chart_serie_temporal", "title": "Serie temporal del consumo",
            "html": "<p style='color:var(--ink-500);font-size:13px'>No disponible: sin columna temporal.</p>",
            "interpretation": "No generada.", "limitations": "Requiere columna de fecha/hora.",
        })

    # 2. Histograma
    if y_col in df.columns:
        charts.append(_chart_histograma(df, y_col))

    # 3. Boxplot
    if y_col in df.columns:
        charts.append(_chart_boxplot(df_num, y_col))

    # 4. Consumo por periodo calendario
    if date_col and date_col in df.columns and y_col in df.columns:
        charts.append(_chart_calendario(df, y_col, date_col))
    else:
        charts.append({
            "id": "chart_calendario", "title": "Consumo por periodo calendario",
            "html": "<p style='color:var(--ink-500);font-size:13px'>No disponible: sin columna temporal.</p>",
            "interpretation": "No generada.", "limitations": "Requiere columna de fecha/hora.",
        })

    # 5. Scatter Y vs cada X (primeras 4)
    valid_x = [c for c in x_cols if c in df.columns and c != y_col]
    for xc in valid_x[:4]:
        charts.append(_chart_scatter(df_num, y_col, xc))

    # 6. Matriz correlación
    corr_cols = [y_col] + [c for c in x_cols if c in df_num.columns and c != y_col]
    if len(corr_cols) >= 2:
        charts.append(_chart_correlacion(df_num, corr_cols))

    # 7. Outliers resaltados
    if y_col in df_num.columns:
        charts.append(_chart_outliers(df, df_num, y_col, date_col))

    # 8. Heatmap temporal (si granularidad horaria/diaria)
    if date_col and date_col in df.columns and y_col in df.columns:
        charts.append(_chart_heatmap_temporal(df, y_col, date_col))

    return charts


def _chart_serie_temporal(df: pd.DataFrame, y_col: str, date_col: str) -> Dict:
    try:
        sub = df[[date_col, y_col]].copy()
        sub[date_col] = pd.to_datetime(sub[date_col], errors="coerce")
        sub[y_col] = pd.to_numeric(sub[y_col], errors="coerce")
        sub = sub.dropna().sort_values(date_col)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=sub[date_col], y=sub[y_col], mode="lines+markers",
                                  line=dict(color="#16a34a", width=1.5),
                                  marker=dict(size=3), name=y_col))
        fig.update_layout(title=f"Serie temporal: {y_col}", xaxis_title="Fecha",
                           yaxis_title=y_col, height=360, margin=dict(l=10, r=10, t=40, b=10))
        return {
            "id": "chart_serie_temporal", "title": f"Serie temporal de {y_col}",
            "html": fig.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True}),
            "figure": fig,
            "interpretation": "Permite identificar tendencias, saltos, huecos y cambios de régimen.",
            "limitations": "Interpretar con conocimiento del proceso operativo.",
        }
    except Exception as e:
        return {"id": "chart_serie_temporal", "title": "Serie temporal",
                "html": f"<p style='color:var(--bad-soft);font-size:13px'>Error: {e}</p>",
                "interpretation": "No generada.", "limitations": str(e)}


def _chart_histograma(df: pd.DataFrame, y_col: str) -> Dict:
    try:
        vals = pd.to_numeric(df[y_col], errors="coerce").dropna()
        fig = go.Figure()
        fig.add_trace(go.Histogram(x=vals, nbinsx=40, histnorm="probability density",
                                    marker_color="#16a34a", opacity=0.75, name="Histograma"))
        try:
            from scipy.stats import gaussian_kde
            kde = gaussian_kde(vals)
            xr = np.linspace(float(vals.min()), float(vals.max()), 200)
            fig.add_trace(go.Scatter(x=xr, y=kde(xr), mode="lines",
                                      line=dict(color="#dc2626", width=2), name="KDE"))
        except Exception:
            pass
        fig.add_vline(x=float(vals.mean()), line_dash="dash", line_color="#2563eb",
                      annotation_text=f"Media: {vals.mean():.2f}")
        fig.add_vline(x=float(vals.median()), line_dash="dot", line_color="#7c3aed",
                      annotation_text=f"Mediana: {vals.median():.2f}")
        fig.update_layout(title=f"Distribución de {y_col}", xaxis_title=y_col,
                           yaxis_title="Densidad", height=360, margin=dict(l=10, r=10, t=40, b=10))
        skew = float(vals.skew())
        interp = f"Asimetría: {skew:.2f}. " + ("Distribución aproximadamente simétrica." if abs(skew) < 0.5
                 else ("Asimetría positiva (cola derecha)." if skew > 0 else "Asimetría negativa (cola izquierda)."))
        return {
            "id": "chart_histograma", "title": f"Histograma de {y_col}",
            "html": fig.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True}),
            "figure": fig,
            "interpretation": interp,
            "limitations": "El histograma no distingue outliers de variabilidad operativa legítima.",
        }
    except Exception as e:
        return {"id": "chart_histograma", "title": "Histograma",
                "html": f"<p style='color:var(--bad-soft);font-size:13px'>Error: {e}</p>",
                "interpretation": "No generada.", "limitations": str(e)}


def _chart_boxplot(df_num: pd.DataFrame, y_col: str) -> Dict:
    try:
        vals = df_num[y_col].dropna()
        fig = go.Figure()
        fig.add_trace(go.Box(y=vals, name=y_col, boxpoints="outliers",
                              marker_color="#16a34a", line_color="#15803d"))
        fig.update_layout(title=f"Boxplot de {y_col}", yaxis_title=y_col,
                           height=360, margin=dict(l=10, r=10, t=40, b=10))
        q1, q3 = float(vals.quantile(0.25)), float(vals.quantile(0.75))
        iqr = q3 - q1
        n_out = int(((vals < q1 - 1.5 * iqr) | (vals > q3 + 1.5 * iqr)).sum())
        return {
            "id": "chart_boxplot", "title": f"Boxplot de {y_col}",
            "html": fig.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True}),
            "figure": fig,
            "interpretation": f"IQR = {iqr:.2f}. Puntos fuera del bigote (×1.5 IQR): {n_out}.",
            "limitations": "Los puntos extremos mostrados requieren validación humana antes de excluir.",
        }
    except Exception as e:
        return {"id": "chart_boxplot", "title": "Boxplot",
                "html": f"<p style='color:var(--bad-soft);font-size:13px'>Error: {e}</p>",
                "interpretation": "No generada.", "limitations": str(e)}


def _chart_calendario(df: pd.DataFrame, y_col: str, date_col: str) -> Dict:
    try:
        sub = df[[date_col, y_col]].copy()
        sub[date_col] = pd.to_datetime(sub[date_col], errors="coerce")
        sub[y_col] = pd.to_numeric(sub[y_col], errors="coerce")
        sub = sub.dropna()
        sub["mes"] = sub[date_col].dt.month
        monthly = sub.groupby("mes")[y_col].mean().reset_index()
        MESES = {1:"Ene",2:"Feb",3:"Mar",4:"Abr",5:"May",6:"Jun",
                 7:"Jul",8:"Ago",9:"Sep",10:"Oct",11:"Nov",12:"Dic"}
        monthly["mes_label"] = monthly["mes"].map(MESES)
        fig = go.Figure()
        fig.add_trace(go.Bar(x=monthly["mes_label"], y=monthly[y_col],
                              marker_color="#16a34a", name="Media mensual"))
        fig.update_layout(title=f"Consumo medio por mes: {y_col}", xaxis_title="Mes",
                           yaxis_title=f"Media {y_col}", autosize=True, height=380, margin=dict(l=10,r=10,t=40,b=10))
        return {
            "id": "chart_calendario", "title": "Consumo por mes",
            "html": fig.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True}),
            "figure": fig,
            "interpretation": "Permite detectar estacionalidad y meses atípicos.",
            "limitations": "Basado en media mensual; no refleja variabilidad intra-mes.",
        }
    except Exception as e:
        return {"id": "chart_calendario", "title": "Consumo por calendario",
                "html": f"<p style='color:var(--bad-soft);font-size:13px'>Error: {e}</p>",
                "interpretation": "No generada.", "limitations": str(e)}


def _chart_scatter(df_num: pd.DataFrame, y_col: str, x_col: str) -> Dict:
    try:
        sub = df_num[[x_col, y_col]].dropna()
        if len(sub) < 3:
            raise ValueError("Datos insuficientes")
        r, p = scipy_stats.pearsonr(sub[x_col], sub[y_col])
        m, b = np.polyfit(sub[x_col], sub[y_col], 1)
        xr = np.linspace(float(sub[x_col].min()), float(sub[x_col].max()), 100)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=sub[x_col], y=sub[y_col], mode="markers",
                                  marker=dict(color="#16a34a", size=5, opacity=0.7), name="Datos"))
        fig.add_trace(go.Scatter(x=xr, y=m * xr + b, mode="lines",
                                  line=dict(color="#dc2626", dash="dash", width=2),
                                  name=f"OLS (r={r:.3f})"))
        fig.update_layout(title=f"{y_col} vs {x_col}  |  r={r:.3f}  p={p:.4f}",
                           xaxis_title=x_col, yaxis_title=y_col,
                           autosize=True, height=380, margin=dict(l=10,r=10,t=40,b=10))
        sig = "significativa" if p < 0.05 else "no significativa"
        return {
            "id": f"chart_scatter_{x_col.replace(' ', '_')}",
            "title": f"Dispersión {y_col} vs {x_col}",
            "html": fig.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True}),
            "figure": fig,
            "interpretation": f"Correlación {sig} (r={r:.3f}, p={p:.4f}). La correlación no implica causalidad.",
            "limitations": "Relación lineal asumida. Verificar con Spearman si hay no-linealidad.",
        }
    except Exception as e:
        return {"id": f"chart_scatter_{x_col}", "title": f"Scatter {y_col} vs {x_col}",
                "html": f"<p style='color:var(--bad-soft);font-size:13px'>Error: {e}</p>",
                "interpretation": "No generada.", "limitations": str(e)}


def _chart_correlacion(df_num: pd.DataFrame, cols: List[str]) -> Dict:
    try:
        sub = df_num[[c for c in cols if c in df_num.columns]].dropna()
        if sub.shape[1] < 2:
            raise ValueError("Insuficientes columnas")
        corr = sub.corr(method="pearson")
        labels = list(corr.columns)
        fig = go.Figure(data=go.Heatmap(
            z=corr.values, x=labels, y=labels, colorscale="RdBu", zmid=0,
            text=[[f"{v:.2f}" for v in row] for row in corr.values],
            texttemplate="%{text}", textfont={"size": 10},
            colorbar=dict(title="r"),
        ))
        fig.update_layout(title="Matriz de correlación de Pearson",
                           autosize=True, height=max(350, len(labels) * 55),
                           margin=dict(l=10,r=10,t=40,b=10))
        return {
            "id": "chart_correlacion", "title": "Matriz de correlación",
            "html": fig.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True}),
            "figure": fig,
            "interpretation": "Identifica relaciones entre variables y posible multicolinealidad (|r| > 0.8 entre X).",
            "limitations": "Pearson mide únicamente relaciones lineales.",
        }
    except Exception as e:
        return {"id": "chart_correlacion", "title": "Matriz de correlación",
                "html": f"<p style='color:var(--bad-soft);font-size:13px'>Error: {e}</p>",
                "interpretation": "No generada.", "limitations": str(e)}


def _chart_outliers(df: pd.DataFrame, df_num: pd.DataFrame, y_col: str,
                    date_col: Optional[str]) -> Dict:
    try:
        vals = df_num[y_col].dropna()
        q1, q3 = float(vals.quantile(0.25)), float(vals.quantile(0.75))
        iqr = q3 - q1
        lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        mean_v, std_v = float(vals.mean()), float(vals.std())
        is_out = (df_num[y_col] < lower) | (df_num[y_col] > upper) | \
                 (((df_num[y_col] - mean_v).abs() / std_v) > 3)
        normal = df_num[y_col][~is_out].dropna()
        outlier = df_num[y_col][is_out].dropna()
        x_normal = df[date_col].iloc[normal.index] if date_col and date_col in df.columns else normal.index
        x_out = df[date_col].iloc[outlier.index] if date_col and date_col in df.columns else outlier.index
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=x_normal, y=normal, mode="markers",
                                  marker=dict(color="#16a34a", size=4, opacity=0.6), name="Normal"))
        fig.add_trace(go.Scatter(x=x_out, y=outlier, mode="markers",
                                  marker=dict(color="#dc2626", size=7, symbol="x"), name="Outlier candidato"))
        fig.update_layout(title=f"Detección de outliers: {y_col}",
                           xaxis_title=date_col or "Índice", yaxis_title=y_col,
                           autosize=True, height=380, margin=dict(l=10,r=10,t=40,b=10))
        return {
            "id": "chart_outliers", "title": "Outliers detectados (candidatos)",
            "html": fig.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True}),
            "figure": fig,
            "interpretation": f"{len(outlier)} candidatos a outlier detectados (IQR×1.5 + Z-score>3). Requieren validación humana.",
            "limitations": "Los puntos marcados son candidatos. Pueden representar condiciones operativas reales.",
        }
    except Exception as e:
        return {"id": "chart_outliers", "title": "Outliers",
                "html": f"<p style='color:var(--bad-soft);font-size:13px'>Error: {e}</p>",
                "interpretation": "No generada.", "limitations": str(e)}


def _chart_heatmap_temporal(df: pd.DataFrame, y_col: str, date_col: str) -> Dict:
    try:
        sub = df[[date_col, y_col]].copy()
        sub[date_col] = pd.to_datetime(sub[date_col], errors="coerce")
        sub[y_col] = pd.to_numeric(sub[y_col], errors="coerce")
        sub = sub.dropna()
        sub["hora"] = sub[date_col].dt.hour
        sub["dia_sem"] = sub[date_col].dt.dayofweek
        if sub["hora"].nunique() < 3:
            # Granularidad no horaria: usar día vs mes
            sub["dia"] = sub[date_col].dt.day
            sub["mes"] = sub[date_col].dt.month
            pivot = sub.groupby(["mes", "dia"])[y_col].mean().unstack(fill_value=0)
            title = f"Heatmap día × mes: {y_col}"
            xlabel, ylabel = "Día del mes", "Mes"
        else:
            DIAS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
            pivot = sub.groupby(["hora", "dia_sem"])[y_col].mean().unstack(fill_value=0)
            pivot.columns = [DIAS[i] for i in pivot.columns if i < 7]
            title = f"Heatmap hora × día semana: {y_col}"
            xlabel, ylabel = "Día semana", "Hora"
        fig = go.Figure(data=go.Heatmap(
            z=pivot.values, x=list(pivot.columns), y=list(pivot.index),
            colorscale="Greens", colorbar=dict(title=y_col),
        ))
        fig.update_layout(title=title, xaxis_title=xlabel, yaxis_title=ylabel,
                           autosize=True, height=380, margin=dict(l=10,r=10,t=40,b=10))
        return {
            "id": "chart_heatmap_temporal", "title": title,
            "html": fig.to_html(full_html=False, include_plotlyjs=False, config={"responsive": True}),
            "figure": fig,
            "interpretation": "Identifica patrones operativos repetidos y periodos anómalos.",
            "limitations": "Basado en media agregada; la variabilidad intra-celda queda oculta.",
        }
    except Exception as e:
        return {"id": "chart_heatmap_temporal", "title": "Heatmap temporal",
                "html": f"<p style='color:var(--bad-soft);font-size:13px'>Error: {e}</p>",
                "interpretation": "No generada.", "limitations": str(e)}


# ---------------------------------------------------------------------------
# E + F. Variables candidatas + correlaciones
# ---------------------------------------------------------------------------

def _variable_analysis(df: pd.DataFrame, df_num: pd.DataFrame,
                        y_col: str, x_cols: List[str]) -> Tuple[List[Dict], List[Dict]]:
    # Clasificación de variables
    var_table = []
    for col in df.columns:
        role = "Objetivo (Y)" if col == y_col else ("Predictora (X)" if col in x_cols else "Desconocida")
        dtype = str(df[col].dtype)
        n_null = int(df[col].isnull().sum())
        pct_null = round(n_null / len(df) * 100, 1) if len(df) > 0 else 0
        tipo_ipmvp = _classify_col_type(col)
        var_table.append({
            "variable": col, "tipo_dato": dtype, "rol_ipmvp": role,
            "tipo_ipmvp": tipo_ipmvp, "pct_nulos": pct_null,
            "obs": "Variable objetivo principal" if col == y_col else "",
        })

    # Correlaciones Pearson + Spearman
    corr_table = []
    y_vals = pd.to_numeric(df[y_col], errors="coerce") if y_col in df.columns else None
    if y_vals is not None:
        for xc in x_cols:
            if xc not in df.columns or xc == y_col:
                continue
            x_vals = pd.to_numeric(df[xc], errors="coerce")
            common = y_vals.dropna().index.intersection(x_vals.dropna().index)
            if len(common) < 3:
                continue
            yc_v = y_vals.loc[common]
            xc_v = x_vals.loc[common]
            try:
                r_p, p_p = scipy_stats.pearsonr(yc_v, xc_v)
                r_s, p_s = scipy_stats.spearmanr(yc_v, xc_v)
                interp = _interpret_corr(r_p)
                rec = "Recomendable" if abs(r_p) > 0.5 else ("Potencialmente útil" if abs(r_p) > 0.3 else "Débil")
                corr_table.append({
                    "variable": xc,
                    "pearson": round(r_p, 4),
                    "spearman": round(r_s, 4),
                    "p_pearson": round(p_p, 5),
                    "n_valido": len(common),
                    "interpretacion": interp,
                    "clasificacion": rec,
                })
            except Exception:
                pass

    corr_table.sort(key=lambda x: abs(x.get("pearson", 0)), reverse=True)
    return var_table, corr_table


def _classify_col_type(col: str) -> str:
    col_l = col.lower()
    if any(k in col_l for k in ["kwh", "kwp", "kww", "consumo", "energia", "energy", "electricidad"]):
        return "consumo"
    if any(k in col_l for k in ["temp", "temperatura", "hdd", "cdd", "clima"]):
        return "clima"
    if any(k in col_l for k in ["prod", "produccion", "unidad", "tonelada", "litro"]):
        return "produccion"
    if any(k in col_l for k in ["fecha", "date", "hora", "time", "timestamp"]):
        return "fecha/hora"
    if any(k in col_l for k in ["ocup", "ocupa", "personal", "turno"]):
        return "ocupacion"
    return "desconocida"


def _interpret_corr(r: float) -> str:
    a = abs(r)
    if a > 0.8:
        return "Muy alta"
    if a > 0.6:
        return "Alta"
    if a > 0.4:
        return "Moderada"
    if a > 0.2:
        return "Baja"
    return "Muy baja / nula"


# ---------------------------------------------------------------------------
# G. Detección de outliers
# ---------------------------------------------------------------------------

def _outlier_analysis(df: pd.DataFrame, df_num: pd.DataFrame, y_col: str,
                       date_col: Optional[str], criterio: str
                       ) -> Tuple[List[Dict], List[int], List[Dict]]:
    outlier_table = []
    outlier_rows = []
    human_val = []

    if y_col not in df_num.columns:
        return outlier_table, outlier_rows, human_val

    vals = df_num[y_col].dropna()
    n = len(vals)
    mean_v = float(vals.mean())
    std_v = float(vals.std())
    q1 = float(vals.quantile(0.25))
    q3 = float(vals.quantile(0.75))
    iqr = q3 - q1

    # IQR
    fence = 1.5 * iqr
    out_iqr = vals[(vals < q1 - fence) | (vals > q3 + fence)]
    outlier_table.append({
        "variable": y_col, "metodo": "IQR × 1.5",
        "umbral": f"< {q1 - fence:.2f}  o  > {q3 + fence:.2f}",
        "n_outliers": len(out_iqr),
        "pct": round(len(out_iqr) / n * 100, 1) if n else 0,
        "valores_destacados": ", ".join([f"{v:.2f}" for v in sorted(out_iqr.values)[:5]]),
        "interpretacion": "Valores fuera del rango intercuartílico típico.",
        "accion": "Requiere validación humana antes de excluir.",
    })

    # Z-score
    if std_v > 0:
        zscores = (vals - mean_v) / std_v
        out_z = vals[zscores.abs() > 3]
        outlier_table.append({
            "variable": y_col, "metodo": "Z-score > 3",
            "umbral": f"|z| > 3  (media={mean_v:.2f}, std={std_v:.2f})",
            "n_outliers": len(out_z),
            "pct": round(len(out_z) / n * 100, 1) if n else 0,
            "valores_destacados": ", ".join([f"{v:.2f}" for v in sorted(out_z.values)[:5]]),
            "interpretacion": "Valores a más de 3 desviaciones estándar.",
            "accion": "Revisar con el responsable del proceso.",
        })

    # Percentiles extremos (1% / 99%)
    p01 = float(vals.quantile(0.01))
    p99 = float(vals.quantile(0.99))
    out_pct = vals[(vals < p01) | (vals > p99)]
    outlier_table.append({
        "variable": y_col, "metodo": "Percentiles 1-99%",
        "umbral": f"< P1={p01:.2f}  o  > P99={p99:.2f}",
        "n_outliers": len(out_pct),
        "pct": round(len(out_pct) / n * 100, 1) if n else 0,
        "valores_destacados": ", ".join([f"{v:.2f}" for v in sorted(out_pct.values)[:5]]),
        "interpretacion": "Valores en los extremos absolutos de la distribución.",
        "accion": "Evaluar si corresponden a eventos reales o errores de medición.",
    })

    # Combinados (IQR ∩ Z-score) — intersección: solo outlier si ambos métodos coinciden
    iqr_mask = (vals < q1 - fence) | (vals > q3 + fence)
    z_mask = pd.Series(False, index=vals.index)
    if std_v > 0:
        z_mask = (vals - mean_v).abs() / std_v > 3
    combined_mask = iqr_mask & z_mask
    outlier_rows = list(vals[combined_mask].index)

    # Human validation items
    if len(outlier_rows) > 0:
        dates_str = ""
        if date_col and date_col in df.columns:
            dates_str = ", ".join([str(df.at[i, date_col]) for i in outlier_rows[:5] if i in df.index])
        human_val.append({
            "item": f"{len(outlier_rows)} outliers detectados en {y_col}",
            "reason": "Valores fuera del rango IQR×1.5 y/o Z-score>3. Pueden ser errores o eventos reales.",
            "columns": y_col,
            "action": f"Revisar individualmente. Fechas afectadas: {dates_str or 'N/A'}",
        })

    return outlier_table, outlier_rows, human_val


# ---------------------------------------------------------------------------
# H. Narrativa Claude
# ---------------------------------------------------------------------------

_SYSTEM_IPMVP = """Eres un analista senior de datos energéticos especializado en M&V e IPMVP.
Tu tarea es generar una narrativa técnica preliminar de calidad de datos para un informe IPMVP.
Responde siempre en español técnico. Sé conciso y apóyate en los datos estadísticos proporcionados.
No inventes columnas ni valores. No calcules ahorros ni construyas línea base.
Limítate a los puntos 1-6 del análisis IPMVP preliminar."""


def _claude_narrative(df: pd.DataFrame, config: IPMVPConfig, y_col: str, x_cols: List[str],
                       date_col: Optional[str], quality_issues: List[Dict],
                       summary_stats: Dict, api_key: Optional[str]) -> str:
    if not _ANTHROPIC_AVAILABLE or not api_key:
        n = len(df)
        n_null = int(df.isnull().sum().sum())
        y_stats = summary_stats.get(y_col, {})
        return (
            f"**Análisis estadístico automático** (Claude API no disponible)\n\n"
            f"Dataset con **{n} registros** y **{len(df.columns)} variables**. "
            f"Total de valores nulos detectados: **{n_null}**. "
            f"Variable objetivo `{y_col}`: media={y_stats.get('mean','N/A')}, "
            f"std={y_stats.get('std','N/A')}, rango=[{y_stats.get('min','N/A')}, {y_stats.get('max','N/A')}]. "
            f"Problemas de calidad identificados: {len(quality_issues)}. "
            f"Variables predictoras candidatas: {len(x_cols)}."
        )
    try:
        client = _anthropic.Anthropic(api_key=api_key)
        stats_str = json.dumps({k: v for k, v in summary_stats.items()}, ensure_ascii=False, indent=2)
        issues_str = json.dumps(quality_issues[:10], ensure_ascii=False, indent=2)
        msg = (
            f"Proyecto: {config.proyecto or 'No especificado'}. "
            f"Variable objetivo: {y_col}. Variables X: {x_cols[:6]}. "
            f"Columna temporal: {date_col or 'No especificada'}.\n\n"
            f"Estadísticas descriptivas:\n```json\n{stats_str}\n```\n\n"
            f"Problemas de calidad detectados:\n```json\n{issues_str}\n```\n\n"
            f"Genera una narrativa técnica de 3-5 párrafos sobre la calidad de los datos "
            f"y las variables candidatas para el análisis IPMVP preliminar."
        )
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=[{"type": "text", "text": _SYSTEM_IPMVP,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": msg}],
        )
        return response.content[0].text
    except Exception as exc:
        return f"Error al conectar con Claude API: {exc}\n\nSe aplica análisis estadístico automático."


# ---------------------------------------------------------------------------
# I. Estado, limitaciones, conclusión
# ---------------------------------------------------------------------------

def _evaluate_dataset_state(quality_issues: List[Dict], n: int) -> str:
    high_sev = [q for q in quality_issues if q.get("severidad") == "alta"]
    if len(high_sev) >= 3 or (high_sev and n < 30):
        return "no_apto"
    if high_sev or len(quality_issues) > 4:
        return "parcialmente_apto"
    return "apto"


def _collect_limitations(df: pd.DataFrame, y_col: str, x_cols: List[str],
                          date_col: Optional[str], charts: List[Dict]) -> List[str]:
    lims = []
    if not date_col or date_col not in df.columns:
        lims.append("Sin columna temporal: no se generaron serie temporal, heatmap ni calendario.")
    if len(x_cols) == 0:
        lims.append("Sin variables X candidatas: no se calcularon correlaciones ni scatter.")
    if len(df) < 30:
        lims.append(f"Dataset pequeño ({len(df)} filas): los resultados estadísticos tienen baja representatividad.")
    return lims


def _build_conclusion(estado: str, n_orig: int, n_man: int, n_exc: int, n_pend: int,
                       var_table: List[Dict], frecuencia: str) -> str:
    estado_label = {"apto": "APTO", "parcialmente_apto": "PARCIALMENTE APTO", "no_apto": "NO APTO"}
    recs = [v for v in var_table if v.get("rol_ipmvp") == "Predictora (X)"]
    return (
        f"El dataset se evalúa como **{estado_label.get(estado, estado)}** para el análisis IPMVP.\n\n"
        f"- Registros originales: {n_orig}\n"
        f"- Registros mantenidos: {n_man}\n"
        f"- Registros excluidos: {n_exc}\n"
        f"- Registros pendientes de revisión: {n_pend}\n"
        f"- Frecuencia detectada: {frecuencia}\n"
        f"- Variables predictoras candidatas: {len(recs)}\n\n"
        "Antes de proceder al modelado IPMVP, es necesario resolver las decisiones de validación humana listadas."
    )


# ---------------------------------------------------------------------------
# Ensamblar tablas y secciones
# ---------------------------------------------------------------------------

def _assemble_tables(df, df_num, y_col, x_cols, date_col, quality_issues,
                     summary_stats, charts, var_table, corr_table,
                     outlier_table, traceability, n_exc, n_pend, n_orig) -> List[Dict]:
    tables = []

    # T1: Estructura
    tables.append({"id": "t1_estructura", "title": "Tabla 1. Archivos y estructura detectada",
                    "columns": ["Hoja/Tabla", "Filas", "Columnas", "Periodo", "Observaciones"],
                    "rows": [["-", str(n_orig), str(len(df.columns)), "-", f"{len(df.columns)} variables detectadas"]]})

    # T2: Inventario de variables
    tables.append({"id": "t2_inventario", "title": "Tabla 2. Inventario de variables",
                    "columns": ["Variable", "Tipo dato", "Tipo IPMVP", "Rol IPMVP", "% Nulos", "Obs"],
                    "rows": [[v["variable"], v["tipo_dato"], v["tipo_ipmvp"], v["rol_ipmvp"],
                               str(v["pct_nulos"]), v["obs"]] for v in var_table]})

    # T3: Calidad
    tables.append({"id": "t3_calidad", "title": "Tabla 3. Calidad de datos",
                    "columns": ["Problema", "Variable", "N registros", "% total", "Severidad", "Acción"],
                    "rows": [[q["problema"], q["variable"], str(q["n_registros"]),
                               str(q["pct"]), q["severidad"], q["accion"]] for q in quality_issues]
                              if quality_issues else [["Sin problemas detectados", "—", "—", "—", "—", "—"]]})

    # T4: Estadística descriptiva
    t4_rows = []
    for col, st in summary_stats.items():
        cv_str = f"{st['cv']:.1f}%" if st.get("cv") is not None else "N/A"
        t4_rows.append([col, str(st["n"]), str(st["mean"]), str(st["median"]),
                         str(st["min"]), str(st["p25"]), str(st["p75"]),
                         str(st["max"]), str(st["std"]), cv_str])
    tables.append({"id": "t4_estadistica", "title": "Tabla 4. Estadística descriptiva",
                    "columns": ["Variable", "N válido", "Media", "Mediana", "Mín", "P25", "P75", "Máx", "Std", "CV"],
                    "rows": t4_rows})

    # T5: Gráficas
    tables.append({"id": "t5_graficas", "title": "Tabla 5. Gráficas generadas",
                    "columns": ["ID", "Título", "Interpretación", "Limitaciones"],
                    "rows": [[c["id"], c["title"], c["interpretation"][:80], c["limitations"][:80]]
                              for c in charts]})

    # T6: Variables candidatas
    tables.append({"id": "t6_candidatas", "title": "Tabla 6. Variables independientes candidatas",
                    "columns": ["Variable", "Tipo", "Rol IPMVP", "Clasificación"],
                    "rows": [[v["variable"], v["tipo_ipmvp"], v["rol_ipmvp"],
                               "Objetivo" if v["rol_ipmvp"] == "Objetivo (Y)" else "Candidata"] for v in var_table]})

    # T7: Correlaciones
    if corr_table:
        tables.append({"id": "t7_correlaciones", "title": "Tabla 7. Correlaciones preliminares",
                        "columns": ["Variable X", "Pearson r", "Spearman r", "N válido", "Interpretación", "Clasificación"],
                        "rows": [[c["variable"], str(c["pearson"]), str(c["spearman"]),
                                   str(c["n_valido"]), c["interpretacion"], c["clasificacion"]]
                                  for c in corr_table]})
    else:
        tables.append({"id": "t7_correlaciones", "title": "Tabla 7. Correlaciones preliminares",
                        "columns": ["Variable X", "Pearson r", "Spearman r", "N válido", "Interpretación", "Clasificación"],
                        "rows": [["Sin variables X válidas", "—", "—", "—", "—", "—"]]})

    # T8: Outliers
    tables.append({"id": "t8_outliers", "title": "Tabla 8. Outliers detectados",
                    "columns": ["Variable", "Método", "Umbral", "N outliers", "% total", "Valores destacados", "Acción"],
                    "rows": [[o["variable"], o["metodo"], o["umbral"], str(o["n_outliers"]),
                               str(o["pct"]), o["valores_destacados"], o["accion"]]
                              for o in outlier_table]})

    # T9: Trazabilidad
    tables.append({"id": "t9_trazabilidad", "title": "Tabla 9. Trazabilidad de conservación de datos",
                    "columns": ["Categoría", "N registros", "% total", "Criterio", "Estado"],
                    "rows": [
                        ["Originales", str(n_orig), "100%", "—", "—"],
                        ["Mantenidos", str(max(0, n_orig - n_exc - n_pend)),
                         f"{max(0, (n_orig - n_exc - n_pend)/n_orig*100):.1f}%" if n_orig else "—", "Sin problemas", "Mantenido"],
                        ["Excluidos (duplicados)", str(n_exc),
                         f"{n_exc/n_orig*100:.1f}%" if n_orig else "—", "Duplicados exactos", "Excluido"],
                        ["Pendientes revisión (outliers)", str(n_pend),
                         f"{n_pend/n_orig*100:.1f}%" if n_orig else "—", "IQR + Z-score", "Pendiente"],
                    ]})

    return tables


def _assemble_sections(df, config, y_col, x_cols, date_col,
                        narrativa_calidad, summary_stats, frecuencia,
                        periodo, estado, parametros_inferidos) -> List[Dict]:
    sections = []
    estado_badge = {
        "apto": '<span class="badge badge-green">APTO</span>',
        "parcialmente_apto": '<span class="badge" style="background:rgba(245,158,11,.12);color:#d97706;border:1px solid rgba(245,158,11,.25)">PARCIALMENTE APTO</span>',
        "no_apto": '<span class="badge badge-red">NO APTO</span>',
    }.get(estado, "")

    _ipmvp_label = f"IPMVP Opción {config.option}"
    if config.note:
        _ipmvp_label += f" {config.note}"
    sections.append({"id": "section-identificacion", "title": "1. Identificación del análisis",
                      "html": f"""
<dl style="display:grid;grid-template-columns:1fr 1fr;gap:10px 24px;font-size:13px">
  <dt style="color:var(--ink-400);font-weight:600">Proyecto</dt><dd style="color:var(--ink-200)">{config.proyecto or '—'}</dd>
  <dt style="color:var(--ink-400);font-weight:600">Cliente / Sitio</dt><dd style="color:var(--ink-200)">{config.cliente_sitio or '—'}</dd>
  <dt style="color:var(--ink-400);font-weight:600">Fecha de análisis</dt><dd style="color:var(--ink-200)">{datetime.now().strftime('%Y-%m-%d %H:%M')}</dd>
  <dt style="color:var(--ink-400);font-weight:600">Periodo detectado</dt><dd style="color:var(--ink-200)">{periodo}</dd>
  <dt style="color:var(--ink-400);font-weight:600">Frecuencia</dt><dd style="color:var(--ink-200)">{frecuencia}</dd>
  <dt style="color:var(--ink-400);font-weight:600">Variable de consumo</dt><dd style="color:var(--ink-200)">{y_col}</dd>
  <dt style="color:var(--ink-400);font-weight:600">Metodología IPMVP</dt><dd style="color:var(--ink-200)">{_ipmvp_label}</dd>
  <dt style="color:var(--ink-400);font-weight:600">Estado del dataset</dt><dd>{estado_badge}</dd>
  <dt style="color:var(--ink-400);font-weight:600">Contexto operacional</dt><dd style="color:var(--ink-200)">{config.contexto_operacional or '—'}</dd>
</dl>"""})

    sections.append({"id": "section-calidad", "title": "4. Calidad de datos y narrativa",
                      "html": f'<div style="font-size:13px;color:var(--ink-300);line-height:1.65">{_md_to_html(narrativa_calidad)}</div>'})

    return sections


def _md_to_html(text: str) -> str:
    """Convert markdown to clean HTML — no ## symbols visible to the user."""
    lines = text.split('\n')
    html_lines = []
    in_list = False
    for line in lines:
        # Headings
        if line.startswith('#### '):
            if in_list: html_lines.append('</ul>'); in_list = False
            html_lines.append(f'<h4 class="doc-h4">{line[5:].strip()}</h4>')
        elif line.startswith('### '):
            if in_list: html_lines.append('</ul>'); in_list = False
            html_lines.append(f'<h3 class="doc-h3">{line[4:].strip()}</h3>')
        elif line.startswith('## '):
            if in_list: html_lines.append('</ul>'); in_list = False
            html_lines.append(f'<h2 class="doc-h2">{line[3:].strip()}</h2>')
        elif line.startswith('# '):
            if in_list: html_lines.append('</ul>'); in_list = False
            html_lines.append(f'<h1 class="doc-h1">{line[2:].strip()}</h1>')
        # List items
        elif line.startswith('- ') or line.startswith('* '):
            if not in_list: html_lines.append('<ul class="doc-list">'); in_list = True
            content = _inline_md(line[2:])
            html_lines.append(f'<li>{content}</li>')
        # Numbered list
        elif re.match(r'^\d+\.\s', line):
            if not in_list: html_lines.append('<ol class="doc-list-ol">'); in_list = True
            content = _inline_md(re.sub(r'^\d+\.\s', '', line))
            html_lines.append(f'<li>{content}</li>')
        # Empty line closes list
        elif not line.strip():
            if in_list: html_lines.append('</ul>'); in_list = False
            html_lines.append('')
        # Normal paragraph
        else:
            if in_list: html_lines.append('</ul>'); in_list = False
            html_lines.append(f'<p class="doc-p">{_inline_md(line)}</p>')
    if in_list:
        html_lines.append('</ul>')
    return '\n'.join(html_lines)


def _inline_md(text: str) -> str:
    text = re.sub(r'\*\*\*(.*?)\*\*\*', r'<strong><em>\1</em></strong>', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.*?)\*', r'<em>\1</em>', text)
    text = re.sub(r'`(.*?)`', r'<code class="doc-code">\1</code>', text)
    return text


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# DOCX export
# ---------------------------------------------------------------------------

def generate_docx(report: IPMVPReport) -> bytes:
    """Generate a Word .docx binary from the report with Calibri font and blue/orange theme."""
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor, Inches
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement
    except ImportError:
        raise RuntimeError("python-docx no instalado.")

    import html as _hl
    import io

    BLUE = RGBColor(0x1F, 0x4E, 0x79)
    ORANGE = RGBColor(0xFF, 0x8C, 0x42)
    ICE = RGBColor(0xD6, 0xEA, 0xF8)

    def _set_font_calibri(run, size_pt=11, bold=False, color=None):
        run.font.name = "Calibri"
        run.font.size = Pt(size_pt)
        run.bold = bold
        if color:
            run.font.color.rgb = color

    def _set_heading_calibri(heading, level=1):
        for run in heading.runs:
            run.font.name = "Calibri"
            if level == 1:
                run.font.color.rgb = BLUE
                run.font.size = Pt(14)
            elif level == 2:
                run.font.color.rgb = BLUE
                run.font.size = Pt(12)
            else:
                run.font.color.rgb = RGBColor(0x2E, 0x74, 0xB5)
                run.font.size = Pt(11)

    def _shade_cell(cell, rgb_hex: str):
        """Apply background shading to a table cell."""
        tc = cell._tc
        tcPr = tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:fill"), rgb_hex)
        shd.set(qn("w:val"), "clear")
        tcPr.append(shd)

    def _docx_clean(html_str: str) -> str:
        text = re.sub(r'<[^>]+>', ' ', html_str)
        text = re.sub(r'\*\*\*(.*?)\*\*\*', r'\1', text)
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
        text = re.sub(r'\*(.*?)\*', r'\1', text)
        text = _hl.unescape(text)
        return re.sub(r'\s+', ' ', text).strip()

    def _add_md_block(doc, raw_text: str) -> None:
        for raw_line in raw_text.split('\n'):
            line = raw_line.strip()
            if not line:
                continue
            clean = _docx_clean(line)
            if not clean:
                continue
            is_bullet = line.startswith('- ') or line.startswith('* ')
            if is_bullet:
                p = doc.add_paragraph(style='List Bullet')
                run = p.add_run(re.sub(r'^[-*]\s+', '', clean))
                _set_font_calibri(run)
            else:
                p = doc.add_paragraph(clean)
                if p.runs:
                    _set_font_calibri(p.runs[0])

    doc = Document()

    # Default font
    try:
        doc.styles['Normal'].font.name = 'Calibri'
        doc.styles['Normal'].font.size = Pt(11)
    except Exception:
        pass

    # ── HTML → DOCX parser (mirrors HTML section rendering) ──────────────────
    def _html_to_docx(html_str: str) -> None:
        """Parse HTML content and append formatted content to the Word document."""
        from html.parser import HTMLParser

        class _P(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.p = None
                self.bold = 0
                self.italic = 0
                self.skip = 0
                self.li_style = None
                self.in_table = False
                self.tbl_rows: list = []
                self.cur_row: list = []
                self.cur_cell: list = []
                self.hdr_row = False

            def _end_p(self):
                self.p = None

            def _cur_p(self, style=None):
                if self.p is None:
                    self.p = doc.add_paragraph(style=style) if style else doc.add_paragraph()
                return self.p

            def handle_starttag(self, tag, attrs):
                tag = tag.lower()
                if tag in ('style', 'script'):
                    self.skip += 1; return
                if self.skip: return
                if self.in_table:
                    if tag == 'tr':
                        self.cur_row = []; self.hdr_row = False
                    elif tag in ('td', 'th'):
                        self.cur_cell = []
                        if tag == 'th': self.hdr_row = True
                    return
                if tag in ('p', 'div', 'section', 'blockquote'):
                    self._end_p()
                elif tag == 'h2':
                    self._end_p(); h = doc.add_heading('', 1); _set_heading_calibri(h, 1); self.p = h
                elif tag == 'h3':
                    self._end_p(); h = doc.add_heading('', 2); _set_heading_calibri(h, 2); self.p = h
                elif tag == 'h4':
                    self._end_p(); h = doc.add_heading('', 3); _set_heading_calibri(h, 3); self.p = h
                elif tag == 'ul':
                    self._end_p(); self.li_style = 'List Bullet'
                elif tag == 'ol':
                    self._end_p(); self.li_style = 'List Number'
                elif tag == 'li':
                    self._end_p(); self.p = doc.add_paragraph(style=self.li_style or 'List Bullet')
                elif tag == 'table':
                    self._end_p(); self.in_table = True; self.tbl_rows = []
                elif tag in ('strong', 'b'):
                    self.bold += 1
                elif tag in ('em', 'i'):
                    self.italic += 1
                elif tag == 'br':
                    if self.p: self.p.add_run('\n')

            def handle_endtag(self, tag):
                tag = tag.lower()
                if tag in ('style', 'script'):
                    self.skip = max(0, self.skip - 1); return
                if self.skip: return
                if self.in_table:
                    if tag in ('td', 'th'):
                        self.cur_row.append(' '.join(self.cur_cell).strip()); self.cur_cell = []
                    elif tag == 'tr':
                        if self.cur_row:
                            self.tbl_rows.append((self.hdr_row, list(self.cur_row)))
                        self.cur_row = []; self.hdr_row = False
                    elif tag == 'table':
                        self.in_table = False
                        if self.tbl_rows:
                            ncols = max(len(r) for _, r in self.tbl_rows)
                            t = doc.add_table(rows=len(self.tbl_rows), cols=ncols)
                            t.style = 'Table Grid'
                            for ri, (is_h, row) in enumerate(self.tbl_rows):
                                for ci, v in enumerate(row[:ncols]):
                                    c = t.rows[ri].cells[ci]; c.text = v
                                    if is_h or ri == 0:
                                        _shade_cell(c, 'D6EAF8')
                                        if c.paragraphs[0].runs:
                                            _set_font_calibri(c.paragraphs[0].runs[0], bold=True, color=BLUE, size_pt=9)
                                    else:
                                        if ri % 2 == 1: _shade_cell(c, 'F4F9FD')
                                        if c.paragraphs[0].runs:
                                            _set_font_calibri(c.paragraphs[0].runs[0], size_pt=9)
                            doc.add_paragraph()
                    return
                if tag in ('p', 'div', 'li', 'section', 'blockquote'):
                    self._end_p()
                elif tag in ('h2', 'h3', 'h4'):
                    self._end_p()
                elif tag in ('ul', 'ol'):
                    self.li_style = None
                elif tag in ('strong', 'b'):
                    self.bold = max(0, self.bold - 1)
                elif tag in ('em', 'i'):
                    self.italic = max(0, self.italic - 1)

            def handle_data(self, data):
                if self.skip: return
                if self.in_table:
                    self.cur_cell.append(data); return
                text = re.sub(r'\s+', ' ', data)
                if not text.strip(): return
                p = self._cur_p()
                run = p.add_run(text)
                _set_font_calibri(run, size_pt=10, bold=(self.bold > 0))
                if self.italic: run.italic = True

        _P().feed(html_str)

    # helper: render one report table as Word table
    def _add_report_table(t: dict) -> None:
        h_t = doc.add_heading(t["title"], 2)
        _set_heading_calibri(h_t, 2)
        if not t["rows"]:
            p = doc.add_paragraph("Sin datos.")
            if p.runs: _set_font_calibri(p.runs[0])
            return
        n_cols = len(t["columns"])
        tbl = doc.add_table(rows=1 + len(t["rows"]), cols=n_cols)
        tbl.style = "Table Grid"
        for i, col in enumerate(t["columns"]):
            c = tbl.rows[0].cells[i]; c.text = col; _shade_cell(c, "D6EAF8")
            if c.paragraphs[0].runs:
                _set_font_calibri(c.paragraphs[0].runs[0], bold=True, color=BLUE, size_pt=9)
        for r_idx, row in enumerate(t["rows"]):
            for c_idx, v in enumerate(row[:n_cols]):
                c = tbl.rows[r_idx + 1].cells[c_idx]; c.text = str(v)
                if (r_idx % 2) == 1: _shade_cell(c, "F4F9FD")
                if c.paragraphs[0].runs:
                    _set_font_calibri(c.paragraphs[0].runs[0], size_pt=9)
        doc.add_paragraph()

    # ── PORTADA (igual que el header HTML) ───────────────────────────────────
    ipmvp_label = f"IPMVP Opción {report.config.option}"
    if report.config.note:
        ipmvp_label += f" {report.config.note}"
    estado_label = {"apto": "APTO", "parcialmente_apto": "PARCIALMENTE APTO",
                    "no_apto": "NO APTO"}.get(report.estado_dataset, report.estado_dataset.upper())

    title_p = doc.add_heading("Análisis Preliminar de Datos", 0)
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in title_p.runs:
        run.font.name = "Calibri"; run.font.color.rgb = BLUE; run.font.size = Pt(20)

    sub1 = doc.add_paragraph(report.config.proyecto or "Sin proyecto especificado")
    sub1.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if sub1.runs: _set_font_calibri(sub1.runs[0], size_pt=13, bold=True, color=BLUE)

    sub2_txt = f"{report.config.cliente_sitio or ''}  ·  {report.fecha_analisis}"
    sub2 = doc.add_paragraph(sub2_txt.strip(" ·").strip())
    sub2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if sub2.runs: _set_font_calibri(sub2.runs[0], size_pt=11, color=RGBColor(0x6B, 0x72, 0x80))

    sub3 = doc.add_paragraph(f"{ipmvp_label}  ·  Estado: {estado_label}")
    sub3.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if sub3.runs: _set_font_calibri(sub3.runs[0], size_pt=11, bold=True, color=ORANGE)

    doc.add_paragraph()

    # ── TRAZABILIDAD (igual que los 4 tiles del HTML) ────────────────────────
    h_traz = doc.add_heading("Trazabilidad de datos", 1)
    _set_heading_calibri(h_traz, 1)
    tbl_traz = doc.add_table(rows=2, cols=4)
    tbl_traz.style = "Table Grid"
    _traz = [
        ("Originales",         str(report.n_filas_original),   "D6EAF8", BLUE),
        ("Mantenidas",         str(report.n_filas_mantenidas),  "D6EAF8", BLUE),
        ("Excluidas",          str(report.n_filas_excluidas),   "FECDD3", RGBColor(0xBE, 0x12, 0x3C)),
        ("Pendientes revisión",str(report.n_filas_pendientes),  "FFE8D4", ORANGE),
    ]
    for i, (lbl, val, shade, col) in enumerate(_traz):
        ch = tbl_traz.rows[0].cells[i]; ch.text = lbl; _shade_cell(ch, "D6EAF8")
        if ch.paragraphs[0].runs: _set_font_calibri(ch.paragraphs[0].runs[0], bold=True, color=BLUE)
        cv = tbl_traz.rows[1].cells[i]; cv.text = val; _shade_cell(cv, shade)
        if cv.paragraphs[0].runs: _set_font_calibri(cv.paragraphs[0].runs[0], size_pt=14, bold=True, color=col)
    doc.add_paragraph()

    # ── PARÁMETROS INFERIDOS (si existen, igual que HTML) ────────────────────
    if report.parametros_inferidos:
        h_pi = doc.add_heading("Parámetros inferidos automáticamente", 1)
        _set_heading_calibri(h_pi, 1)
        pi_hdrs = ["Parámetro", "Valor inferido", "Evidencia", "Confianza"]
        tbl_pi = doc.add_table(rows=1 + len(report.parametros_inferidos), cols=4)
        tbl_pi.style = "Table Grid"
        for i, lbl in enumerate(pi_hdrs):
            c = tbl_pi.rows[0].cells[i]; c.text = lbl; _shade_cell(c, "D6EAF8")
            if c.paragraphs[0].runs: _set_font_calibri(c.paragraphs[0].runs[0], bold=True, color=BLUE, size_pt=9)
        for ri, p_inf in enumerate(report.parametros_inferidos):
            for ci, key in enumerate(["parametro", "valor", "evidencia", "confianza"]):
                c = tbl_pi.rows[ri + 1].cells[ci]; c.text = str(p_inf.get(key, ""))
                if c.paragraphs[0].runs: _set_font_calibri(c.paragraphs[0].runs[0], size_pt=9)
        doc.add_paragraph()

    # ── JUSTIFICACIÓN DE EXCLUSIONES (siempre desde report.justification_*) ──
    # Renderizado fuera del bucle de secciones para que funcione tanto desde
    # el endpoint HTML (que inserta section-justificacion) como desde el
    # endpoint de descarga (que NO inserta esa sección).
    excl = report.justification_excluded
    pend = report.justification_pending

    if excl or pend:
        h_j = doc.add_heading("Justificación de eliminación de datos", 1)
        _set_heading_calibri(h_j, 1)
        # Summary
        tbl_sum = doc.add_table(rows=2, cols=4)
        tbl_sum.style = "Table Grid"
        _n_orig = report.n_filas_original
        _n_excl = len(excl)
        _n_pend = len(pend)
        _n_keep = _n_orig - _n_excl
        for i, (lbl, val) in enumerate([
            ("Filas originales", str(_n_orig)),
            ("Mantenidas",       str(_n_keep)),
            ("Excluidas",        str(_n_excl)),
            ("Pendientes",       str(_n_pend)),
        ]):
            ch = tbl_sum.rows[0].cells[i]; ch.text = lbl; _shade_cell(ch, "D6EAF8")
            if ch.paragraphs[0].runs: _set_font_calibri(ch.paragraphs[0].runs[0], bold=True, color=BLUE)
            cv = tbl_sum.rows[1].cells[i]; cv.text = val
            if cv.paragraphs[0].runs: _set_font_calibri(cv.paragraphs[0].runs[0])
        doc.add_paragraph()
        # Resumen por causa — excluidas
        if excl:
            p_rsm = doc.add_paragraph("Resumen por causa — filas excluidas")
            if p_rsm.runs: _set_font_calibri(p_rsm.runs[0], bold=True)
            reason_counts: dict = {}
            for item in excl:
                reason_counts[item["reason"]] = reason_counts.get(item["reason"], 0) + 1
            tbl_rc = doc.add_table(rows=1 + len(reason_counts), cols=2)
            tbl_rc.style = "Table Grid"
            for i, lbl in enumerate(["Causa", "N"]):
                c = tbl_rc.rows[0].cells[i]; c.text = lbl; _shade_cell(c, "D6EAF8")
                if c.paragraphs[0].runs: _set_font_calibri(c.paragraphs[0].runs[0], bold=True, color=BLUE)
            for r_idx, (reason, count) in enumerate(sorted(reason_counts.items(), key=lambda x: -x[1])):
                tbl_rc.rows[r_idx + 1].cells[0].text = reason
                tbl_rc.rows[r_idx + 1].cells[1].text = str(count)
                for ci in range(2):
                    if tbl_rc.rows[r_idx + 1].cells[ci].paragraphs[0].runs:
                        _set_font_calibri(tbl_rc.rows[r_idx + 1].cells[ci].paragraphs[0].runs[0])
            doc.add_paragraph()
        # T-Justif — listado completo
        if excl:
            p_tj = doc.add_paragraph("T-Justif — Listado completo de filas excluidas del análisis")
            if p_tj.runs: _set_font_calibri(p_tj.runs[0], bold=True)
            _excl_hdrs = ["Fila original", "Fecha / Hora",
                           f"Valor {report.config.variable_consumo or 'Y'}", "Método de detección"]
            tbl_excl = doc.add_table(rows=1 + len(excl), cols=4)
            tbl_excl.style = "Table Grid"
            for i, lbl in enumerate(_excl_hdrs):
                c = tbl_excl.rows[0].cells[i]; c.text = lbl; _shade_cell(c, "D6EAF8")
                if c.paragraphs[0].runs: _set_font_calibri(c.paragraphs[0].runs[0], bold=True, color=BLUE)
            for r_idx, item in enumerate(excl):
                _yv = f"{item['y_value']:.4g}" if item.get("y_value") is not None else "—"
                _dt = str(item.get("date") or "—")
                _sev = f" [{item['severity'].upper()}]" if item.get("severity") else ""
                row_vals = [str(item["idx"]), _dt, _yv, item["reason"] + _sev]
                for ci, val in enumerate(row_vals):
                    cell = tbl_excl.rows[r_idx + 1].cells[ci]; cell.text = val
                    if (r_idx % 2) == 1: _shade_cell(cell, "F4F9FD")
                    if cell.paragraphs[0].runs: _set_font_calibri(cell.paragraphs[0].runs[0], size_pt=9)
            doc.add_paragraph()
        # T-Pendiente
        if pend:
            p_tp = doc.add_paragraph("T-Pendiente — Filas conservadas pese a aviso automático")
            if p_tp.runs: _set_font_calibri(p_tp.runs[0], bold=True)
            _pend_hdrs = ["Fila original", "Fecha / Hora",
                           f"Valor {report.config.variable_consumo or 'Y'}", "Aviso detectado"]
            tbl_pend = doc.add_table(rows=1 + len(pend), cols=4)
            tbl_pend.style = "Table Grid"
            for i, lbl in enumerate(_pend_hdrs):
                c = tbl_pend.rows[0].cells[i]; c.text = lbl; _shade_cell(c, "FFF3CD")
                if c.paragraphs[0].runs:
                    _set_font_calibri(c.paragraphs[0].runs[0], bold=True, color=RGBColor(0x92, 0x40, 0x00))
            for r_idx, item in enumerate(pend):
                _yv = f"{item['y_value']:.4g}" if item.get("y_value") is not None else "—"
                _dt = str(item.get("date") or "—")
                row_vals = [str(item["idx"]), _dt, _yv, item["reason"]]
                for ci, val in enumerate(row_vals):
                    cell = tbl_pend.rows[r_idx + 1].cells[ci]; cell.text = val
                    if (r_idx % 2) == 1: _shade_cell(cell, "FFFBF0")
                    if cell.paragraphs[0].runs: _set_font_calibri(cell.paragraphs[0].runs[0], size_pt=9)
            doc.add_paragraph()

    # ── SECCIONES (mismo orden que el HTML; section-justificacion se salta) ──
    for sec in report.sections:
        sec_id    = sec.get("id", "")
        sec_title = sec.get("title", "")
        if sec_id == "section-justificacion":
            continue  # ya renderizado arriba desde report.justification_excluded
        if sec_title:
            h_sec = doc.add_heading(sec_title, 1)
            _set_heading_calibri(h_sec, 1)
        _html_to_docx(sec.get("html", ""))
        doc.add_paragraph()

    # ── TABLAS T1–T4 (antes de gráficas, igual que HTML) ────────────────────
    for t in report.tables[:4]:
        _add_report_table(t)

    # ── GRÁFICAS (PNG via kaleido; se omiten si no disponible) ───────────────
    # El titulo solo se anade tras la PRIMERA grafica que rasterice con
    # exito -- antes se anadia siempre, así que un fallo total dejaba un
    # apartado "Gráficas de exploración" vacio, sin ninguna imagen y sin
    # ningun error visible en ningun sitio.
    if report.charts and _kaleido_ok:
        _heading_added = False
        for c in report.charts:
            fig = c.get("figure")
            if fig is None: continue
            try:
                png_bytes = fig.to_image(format="png", width=1000, height=500, scale=1.5)
                if not _heading_added:
                    h_ch = doc.add_heading("Gráficas de exploración", 1)
                    _set_heading_calibri(h_ch, 1)
                    _heading_added = True
                h_fig = doc.add_heading(c["title"], 3)
                _set_heading_calibri(h_fig, 3)
                doc.add_picture(io.BytesIO(png_bytes), width=Inches(6.0))
                if c.get("interpretation"):
                    p_i = doc.add_paragraph(c["interpretation"])
                    if p_i.runs:
                        _set_font_calibri(p_i.runs[0], size_pt=10, color=RGBColor(0x6B, 0x72, 0x80))
            except Exception as _chart_exc:
                import logging as _logging
                _logging.getLogger("palbe").error(
                    "no se pudo anadir la grafica '%s' al informe IPMVP: %s",
                    c.get("title", "?"), _chart_exc,
                )
        if _heading_added:
            doc.add_paragraph()

    # ── TABLAS T5–T9 (después de gráficas, igual que HTML) ──────────────────
    for t in report.tables[4:]:
        _add_report_table(t)

    # ── VALIDACIÓN HUMANA (tabla, igual que HTML) ────────────────────────────
    if report.human_validation:
        h_hv = doc.add_heading("Decisiones que requieren validación humana", 1)
        _set_heading_calibri(h_hv, 1)
        hv_hdrs = ["Ítem", "Razón", "Variables afectadas", "Acción recomendada"]
        tbl_hv = doc.add_table(rows=1 + len(report.human_validation), cols=4)
        tbl_hv.style = "Table Grid"
        for i, lbl in enumerate(hv_hdrs):
            c = tbl_hv.rows[0].cells[i]; c.text = lbl; _shade_cell(c, "D6EAF8")
            if c.paragraphs[0].runs: _set_font_calibri(c.paragraphs[0].runs[0], bold=True, color=BLUE, size_pt=9)
        for ri, hv in enumerate(report.human_validation):
            for ci, key in enumerate(["item", "reason", "columns", "action"]):
                c = tbl_hv.rows[ri + 1].cells[ci]; c.text = str(hv.get(key, ""))
                if ri % 2 == 1: _shade_cell(c, "F4F9FD")
                if c.paragraphs[0].runs: _set_font_calibri(c.paragraphs[0].runs[0], size_pt=9)
        doc.add_paragraph()

    # ── LIMITACIONES ────────────────────────────────────────────────────────
    if report.limitations:
        h_lim = doc.add_heading("Limitaciones", 1)
        _set_heading_calibri(h_lim, 1)
        for lim in report.limitations:
            p_lim = doc.add_paragraph(style="List Bullet")
            run = p_lim.add_run(lim)
            _set_font_calibri(run, size_pt=10)
        doc.add_paragraph()

    # ── CONCLUSIÓN (igual que HTML) ──────────────────────────────────────────
    h_conc = doc.add_heading("Conclusión preliminar y recomendación", 1)
    _set_heading_calibri(h_conc, 1)
    p_est = doc.add_paragraph(f"Estado del dataset: {estado_label}")
    _estado_col = {"apto": RGBColor(0x16, 0xA3, 0x4A),
                   "parcialmente_apto": RGBColor(0xCA, 0x8A, 0x04),
                   "no_apto": RGBColor(0xDC, 0x26, 0x26)}.get(report.estado_dataset, BLUE)
    if p_est.runs: _set_font_calibri(p_est.runs[0], bold=True, color=_estado_col, size_pt=12)
    _html_to_docx(_md_to_html(report.conclusion))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HTML constants
# ---------------------------------------------------------------------------

TABLE_CSS = "glass-table"
TH_CSS = ""
TD_CSS = ""

# Document-like typography CSS (injected once in the report page)
DOC_CSS = """
<style>
/* ── Document layout ────────────────────────────────── */
.doc-page {
  background: #ffffff;
  max-width: 900px;
  margin: 0 auto;
  padding: 48px 64px;
  font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif;
  font-size: 14px;
  line-height: 1.75;
  color: #222222;
  box-shadow: 0 1px 8px rgba(0,0,0,.08);
  border-radius: 4px;
}
@media (max-width: 960px) {
  .doc-page { padding: 28px 20px; }
}
.doc-h1 { font-size: 1.7rem; font-weight: 700; margin: 1.6rem 0 .6rem; border-bottom: 2px solid #FF8C42; padding-bottom: .3rem; color: #1F4E79; }
.doc-h2 { font-size: 1.2rem; font-weight: 700; margin: 1.4rem 0 .4rem; color: #1F4E79; }
.doc-h3 { font-size: 1rem; font-weight: 600; margin: 1rem 0 .3rem; color: #2E74B5; }
.doc-h4 { font-size: .95rem; font-weight: 600; margin: .8rem 0 .2rem; color: #374151; }
.doc-p  { margin: 0 0 .75rem; }
.doc-list    { margin: .4rem 0 .8rem 1.4rem; list-style: disc; }
.doc-list-ol { margin: .4rem 0 .8rem 1.4rem; list-style: decimal; }
.doc-list li, .doc-list-ol li { margin-bottom: .25rem; }
.doc-code { background: #f3f4f6; padding: 1px 5px; border-radius: 3px; font-family: monospace; font-size: .85em; }
/* ── Section headings (numbered) ───────────────────── */
.doc-section-title {
  font-size: 1.1rem; font-weight: 700; color: #1F4E79;
  border-bottom: 1px solid #BFD9EE; padding-bottom: .3rem;
  margin: 2rem 0 .8rem;
}
/* ── Editable regions ──────────────────────────────── */
[contenteditable="true"] {
  outline: none;
  border-radius: 4px;
  transition: background .15s;
  min-height: 1em;
}
[contenteditable="true"]:hover  { background: #EBF5FB; }
[contenteditable="true"]:focus  { background: #EBF5FB; box-shadow: 0 0 0 2px #BFD9EE; }
/* ── Charts ────────────────────────────────────────── */
.doc-chart-wrap {
  width: 100%;
  margin: 1rem 0 .5rem;
}
.doc-chart-wrap > div { width: 100% !important; }
/* ── Tables ────────────────────────────────────────── */
.doc-table-wrap { overflow-x: auto; margin: .8rem 0 1.4rem; border-left: 3px solid #FF8C42; padding-left: 4px; }
.doc-table { width: 100%; border-collapse: collapse; font-size: .82rem; }
.doc-table thead th { background: #D6EAF8; color: #1F4E79; font-weight: 600; text-align: left;
                padding: 6px 10px; border: 1px solid #BFD9EE; }
.doc-table td { padding: 5px 10px; border: 1px solid #E5EEF7; color: #222222; }
.doc-table tr:nth-child(even) td { background: #F4F9FD; }
.doc-table td[contenteditable="true"]:hover { background: #D6EAF8; }
.doc-table td[contenteditable="true"]:focus { background: #EBF5FB; box-shadow: inset 0 0 0 1px #2E74B5; }
.doc-table td.cell-error { background: rgba(239,68,68,.12) !important; color: #dc2626 !important; }
.doc-editable-table .doc-table-actions { display: none; margin-top: 6px; gap: 6px; }
.doc-editable-table.edit-active .doc-table-actions { display: flex; }
.doc-table-actions button { font-size: .75rem; padding: 3px 8px; border-radius: 4px;
  border: 1px solid #BFD9EE; background: #EBF5FB; color: #1F4E79; cursor: pointer; }
.doc-table-actions button:hover { border-color: #FF8C42; color: #FF8C42; }
.doc-table .row-delete-btn { visibility: hidden; padding: 2px 6px; font-size: .7rem;
  border: 1px solid rgba(239,68,68,.3); background: rgba(239,68,68,.08);
  color: #dc2626; border-radius: 3px; cursor: pointer; }
.doc-editable-table.edit-active .row-delete-btn { visibility: visible; }
/* ── Status badge ──────────────────────────────────── */
.doc-badge-apto     { background:#D6EAF8; color:#1F4E79; border: 1px solid #BFD9EE; padding: 2px 10px; border-radius: 20px; }
.doc-badge-parcial  { background:#fef9c3; color:#854d0e; border: 1px solid #fde68a; padding: 2px 10px; border-radius: 20px; }
.doc-badge-no_apto  { background:#fee2e2; color:#991b1b; border: 1px solid #fecaca; padding: 2px 10px; border-radius: 20px; }
/* ── Toolbar ───────────────────────────────────────── */
#doc-toolbar {
  position: sticky; top: 0; z-index: 100;
  background: #1F4E79; border-bottom: 1px solid #17406b;
  padding: 8px 16px; display: flex; align-items: center; gap: 10px;
  flex-wrap: wrap;
  box-shadow: 0 2px 6px rgba(0,0,0,.18);
}
#doc-toolbar button {
  font-size: .78rem; padding: 4px 10px; border-radius: 5px;
  border: 1px solid rgba(255,255,255,.25); background: rgba(255,255,255,.1); cursor: pointer;
  color: #ffffff; transition: all .15s;
}
#doc-toolbar button:hover { border-color: #FF8C42; color: #FF8C42; background: rgba(255,140,66,.1); }
#doc-toolbar .tb-sep { width: 1px; height: 20px; background: rgba(255,255,255,.2); }
#doc-toolbar .tb-edit-on  { background: #FF8C42; border-color: #e87a35; color: #fff; font-weight: 600; }
#edit-status { font-size: .75rem; color: rgba(255,255,255,.7); }
/* ── Print ─────────────────────────────────────────── */
@media print {
  #doc-toolbar, .doc-no-print { display: none !important; }
  .doc-page { box-shadow: none; padding: 0; max-width: 100%; }
  body { background: #fff; }
  .doc-chart-wrap > div { page-break-inside: avoid; }
}
</style>"""


def _render_table(t: Dict) -> str:
    tid = t.get("id", "t_unknown")
    cols_html = "".join(f'<th class="doc-table-th">{c}</th>' for c in t["columns"]) + '<th class="doc-table-th doc-no-print" style="width:32px"></th>'
    rows_html = ""
    for row in t["rows"]:
        cells = "".join(f'<td class="doc-table-td">{cell}</td>' for cell in row)
        rows_html += f'<tr>{cells}<td class="doc-no-print" style="padding:2px 4px;vertical-align:middle"><button class="row-delete-btn" onclick="deleteTableRow(this)">✕</button></td></tr>'
    # Simple schema: all columns default to 'text' unless name hints at numeric
    import re as _re
    schema_cols = {}
    for col in t["columns"]:
        col_lower = col.lower()
        if any(k in col_lower for k in ["n ", "nº", "num", "count", "filas", "%", "pct", "r²", "r2", "rmse", "cv", "mae", "nmbe", "valor", "media", "std", "min", "max", "p25", "p75"]):
            schema_cols[col] = "float"
        else:
            schema_cols[col] = "text"
    import json as _json
    schema_json = _json.dumps(schema_cols).replace('"', '&quot;')
    return f"""
<div class="doc-table-wrap doc-editable-table" data-table-id="{tid}" data-schema="{schema_json}">
  <p class="doc-table-title" style="font-weight:600;font-size:.85rem;color:#1F4E79;margin:0 0 6px">{t['title']}</p>
  <table class="doc-table">
    <thead><tr>{cols_html}</tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <div class="doc-table-actions doc-no-print">
    <button onclick="addTableRow(this)">+ Fila</button>
    <button onclick="recalcTable(this)">↺ Recalcular</button>
  </div>
</div>"""


def render_ipmvp_html(report: IPMVPReport) -> str:
    """Render the full IPMVP report as a Word-like document with editing and download."""
    estado_label = {"apto": "APTO", "parcialmente_apto": "PARCIALMENTE APTO",
                    "no_apto": "NO APTO"}.get(report.estado_dataset, "—")
    badge_cls = {"apto": "doc-badge-apto", "parcialmente_apto": "doc-badge-parcial",
                 "no_apto": "doc-badge-no_apto"}.get(report.estado_dataset, "")

    # --- Parámetros inferidos ---
    inferidos_html = ""
    if report.parametros_inferidos:
        rows = "".join(
            f"<tr><td>{p['parametro']}</td><td>{p['valor']}</td>"
            f"<td>{p['evidencia']}</td><td>{p['confianza']}</td></tr>"
            for p in report.parametros_inferidos)
        inferidos_html = f"""
<div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:14px;margin-bottom:20px;">
  <p style="font-weight:600;color:#1e40af;margin:0 0 8px;">Parámetros inferidos automáticamente</p>
  <table class="doc-table"><thead><tr>
    <th>Parámetro</th><th>Valor inferido</th><th>Evidencia</th><th>Confianza</th>
  </tr></thead><tbody>{rows}</tbody></table>
</div>"""

    # --- IPMVP label ---
    _ipmvp_label = f"IPMVP Opción {report.config.option}"
    if report.config.note:
        _ipmvp_label += f" {report.config.note}"

    # --- Portada / encabezado ---
    header_html = f"""
<div style="text-align:center;margin-bottom:2rem;padding-bottom:1.5rem;border-bottom:3px solid #FF8C42;">
  <h1 style="font-size:1.8rem;font-weight:700;color:#1F4E79;margin:0 0 .4rem;font-family:'Calibri','Carlito','Segoe UI',sans-serif;">
    Análisis Preliminar de Datos
  </h1>
  <p style="font-size:1rem;color:#374151;margin:0 0 .3rem;">
    {report.config.proyecto or 'Sin proyecto especificado'}
  </p>
  <p style="font-size:.88rem;color:#6b7280;margin:0;">
    {report.config.cliente_sitio or ''} &nbsp;·&nbsp; {report.fecha_analisis}
  </p>
  <div style="margin-top:.7rem;display:inline-flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:center">
    <span style="padding:4px 14px;border-radius:20px;font-weight:700;font-size:.85rem;background:#D6EAF8;color:#1F4E79;border:1px solid #BFD9EE">{_ipmvp_label}</span>
    <span style="padding:4px 14px;border-radius:20px;font-weight:700;font-size:.85rem;" class="{badge_cls}">{estado_label}</span>
  </div>
</div>"""

    # --- Trazabilidad numérica ---
    traz_html = f"""
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:2rem;">
  <div style="background:#f9fafb;border:1px solid #E5EEF7;border-radius:8px;padding:14px;text-align:center;">
    <div style="font-size:1.6rem;font-weight:700;color:#374151;">{report.n_filas_original}</div>
    <div style="font-size:.78rem;color:#6b7280;">Originales</div>
  </div>
  <div style="background:#D6EAF8;border:1px solid #BFD9EE;border-radius:8px;padding:14px;text-align:center;">
    <div style="font-size:1.6rem;font-weight:700;color:#1F4E79;">{report.n_filas_mantenidas}</div>
    <div style="font-size:.78rem;color:#6b7280;">Mantenidas</div>
  </div>
  <div style="background:#fff1f2;border:1px solid #fecdd3;border-radius:8px;padding:14px;text-align:center;">
    <div style="font-size:1.6rem;font-weight:700;color:#be123c;">{report.n_filas_excluidas}</div>
    <div style="font-size:.78rem;color:#6b7280;">Excluidas</div>
  </div>
  <div style="background:#FFF8F3;border:1px solid #FFD4B0;border-radius:8px;padding:14px;text-align:center;">
    <div style="font-size:1.6rem;font-weight:700;color:#FF8C42;">{report.n_filas_pendientes}</div>
    <div style="font-size:.78rem;color:#6b7280;">Pendientes revisión</div>
  </div>
</div>"""

    # --- Secciones de texto (editables) ---
    sections_html = ""
    for sec in report.sections:
        sections_html += f"""
<section id="{sec['id']}" style="margin-bottom:2rem;">
  <h2 class="doc-section-title">{sec['title']}</h2>
  <div class="doc-editable" contenteditable="false" data-section="{sec['id']}">{sec['html']}</div>
</section>"""

    # --- Tablas T1–T4 ---
    tables_early = "".join(_render_table(t) for t in report.tables[:4])

    # --- Gráficas ---
    charts_html = ""
    for c in report.charts:
        charts_html += f"""
<figure style="margin:0 0 2rem;" id="figure_{c['id']}">
  <p style="font-weight:600;font-size:.92rem;color:#374151;margin:0 0 6px;">{c['title']}</p>
  <div class="doc-chart-wrap">{c.get('html','')}</div>
  <figcaption style="font-size:.8rem;color:#6b7280;margin-top:6px;font-style:italic;">
    <strong style="font-style:normal;">Interpretación:</strong> {c.get('interpretation','')}
    {'<br><em>Limitación: ' + c.get('limitations','') + '</em>' if c.get('limitations') else ''}
  </figcaption>
</figure>"""

    # --- Tablas T5–T9 ---
    tables_late = "".join(_render_table(t) for t in report.tables[4:])

    # --- Validación humana ---
    hv_rows = "".join(
        f"<tr><td>{h['item']}</td><td>{h['reason']}</td>"
        f"<td>{h['columns']}</td><td>{h['action']}</td></tr>"
        for h in report.human_validation
    ) or "<tr><td colspan='4' style='color:#9ca3af;'>Sin ítems detectados.</td></tr>"

    human_html = f"""
<section id="section-validacion-humana" style="margin-bottom:2rem;">
  <h2 class="doc-section-title">11. Decisiones que requieren validación humana</h2>
  <div class="doc-table-wrap"><table class="doc-table"><thead><tr>
    <th>Ítem</th><th>Razón</th><th>Variables afectadas</th><th>Acción recomendada</th>
  </tr></thead><tbody>{hv_rows}</tbody></table></div>
</section>"""

    # --- Limitaciones ---
    lims_items = "".join(f"<li>{l}</li>" for l in report.limitations) or \
                 "<li style='color:#9ca3af;'>Sin limitaciones adicionales.</li>"

    # --- Conclusión ---
    conclusion_html = f"""
<section id="section-conclusion" style="margin-bottom:2rem;">
  <h2 class="doc-section-title">12. Conclusión preliminar y recomendación</h2>
  <div style="background:{'#f0fdf4' if report.estado_dataset=='apto' else ('#fefce8' if 'parcial' in report.estado_dataset else '#fff1f2')};
    border-left:4px solid {'#16a34a' if report.estado_dataset=='apto' else ('#ca8a04' if 'parcial' in report.estado_dataset else '#dc2626')};
    padding:12px 16px;border-radius:0 6px 6px 0;margin-bottom:12px;">
    <strong>Estado del dataset: {estado_label}</strong>
  </div>
  <div class="doc-editable" contenteditable="false" data-section="section-conclusion">
    {_md_to_html(report.conclusion)}
  </div>
</section>"""

    # --- Pie del documento: botones de descarga ---
    footer_html = """
<div id="doc-footer" class="doc-no-print"
  style="border-top:2px solid #e5e7eb;margin-top:3rem;padding-top:1.5rem;
         display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
  <span style="font-size:.85rem;color:#6b7280;">Exportar informe</span>
  <div style="display:flex;gap:10px;flex-wrap:wrap;">
    <button onclick="downloadDocx()"
      style="padding:8px 18px;background:#1d4ed8;color:#fff;border:none;border-radius:6px;
             font-size:.85rem;font-weight:600;cursor:pointer;">
      ⬇ Descargar Word (.docx)
    </button>
    <button onclick="window.print()"
      style="padding:8px 18px;background:#dc2626;color:#fff;border:none;border-radius:6px;
             font-size:.85rem;font-weight:600;cursor:pointer;">
      ⬇ Imprimir / Guardar PDF
    </button>
  </div>
</div>"""

    # --- Toolbar de edición (sticky) ---
    toolbar_html = """
<div id="doc-toolbar" class="doc-no-print">
  <span style="font-size:.8rem;font-weight:600;color:#fff;">Análisis Preliminar</span>
  <div class="tb-sep"></div>
  <button id="btn-edit" onclick="toggleEdit()">✏ Activar edición</button>
  <button onclick="execFmt('bold')" title="Negrita"><strong>B</strong></button>
  <button onclick="execFmt('italic')" title="Cursiva"><em>I</em></button>
  <button onclick="execFmt('underline')" title="Subrayado"><u>U</u></button>
  <div class="tb-sep"></div>
  <button onclick="saveEdits()" title="Guardar cambios en servidor">💾 Guardar</button>
  <button onclick="downloadDocx()">⬇ Word</button>
  <button onclick="window.print()">⬇ PDF</button>
  <div class="tb-sep"></div>
  <span id="edit-status">Solo lectura</span>
</div>
<script>
let _editMode = false;

function toggleEdit() {
  _editMode = !_editMode;
  document.querySelectorAll('.doc-editable').forEach(el => {
    el.contentEditable = _editMode ? 'true' : 'false';
  });
  enableTableEdit(_editMode);
  const btn = document.getElementById('btn-edit');
  const st  = document.getElementById('edit-status');
  if (_editMode) {
    btn.textContent = '🔒 Desactivar edición';
    btn.classList.add('tb-edit-on');
    st.textContent  = 'Modo edición activo';
    st.style.color  = '#FF8C42';
  } else {
    btn.textContent = '✏ Activar edición';
    btn.classList.remove('tb-edit-on');
    st.textContent  = 'Solo lectura';
    st.style.color  = 'rgba(255,255,255,.7)';
  }
}

function enableTableEdit(on) {
  document.querySelectorAll('.doc-editable-table').forEach(wrap => {
    if (on) {
      wrap.classList.add('edit-active');
      wrap.querySelectorAll('tbody td:not(.doc-no-print)').forEach(td => {
        td.contentEditable = 'true';
        td.addEventListener('input', validateCell);
      });
    } else {
      wrap.classList.remove('edit-active');
      wrap.querySelectorAll('tbody td').forEach(td => {
        td.contentEditable = 'false';
        td.classList.remove('cell-error');
      });
    }
  });
}

function validateCell(e) {
  const td = e.target;
  const wrap = td.closest('.doc-editable-table');
  if (!wrap) return;
  const schema = JSON.parse(wrap.dataset.schema || '{}');
  const thead = wrap.querySelector('thead tr');
  const colIdx = Array.from(td.parentElement.cells).indexOf(td);
  const colName = thead ? (thead.cells[colIdx] ? thead.cells[colIdx].textContent.trim() : '') : '';
  const colType = schema[colName] || 'text';
  if (colType === 'float' || colType === 'int') {
    const v = td.textContent.trim().replace(',', '.');
    if (v !== '' && isNaN(parseFloat(v))) {
      td.classList.add('cell-error');
      td.title = 'Debe ser numérico';
    } else {
      td.classList.remove('cell-error');
      td.title = '';
    }
  }
}

function addTableRow(btn) {
  const wrap = btn.closest('.doc-editable-table');
  const tbody = wrap.querySelector('tbody');
  const lastRow = tbody.lastElementChild;
  if (!lastRow) return;
  const newRow = lastRow.cloneNode(true);
  newRow.querySelectorAll('td:not(.doc-no-print)').forEach(td => {
    td.textContent = '';
    td.classList.remove('cell-error');
    if (_editMode) {
      td.contentEditable = 'true';
      td.addEventListener('input', validateCell);
    }
  });
  tbody.appendChild(newRow);
}

function deleteTableRow(btn) {
  const tr = btn.closest('tr');
  const tbody = tr.parentElement;
  if (tbody.rows.length > 1) tr.remove();
}

function recalcTable(btn) {
  const wrap = btn.closest('.doc-editable-table');
  const schema = JSON.parse(wrap.dataset.schema || '{}');
  const thead = wrap.querySelector('thead tr');
  const tbody = wrap.querySelector('tbody');
  if (!thead || !tbody) return;
  const cols = Array.from(thead.cells).map(th => th.textContent.trim()).filter(c => c !== '');
  // Find numeric columns and recalculate totals / percentages if present
  const numCols = cols.filter(c => schema[c] === 'float' || schema[c] === 'int');
  numCols.forEach(colName => {
    const colIdx = cols.indexOf(colName);
    if (colIdx < 0) return;
    const rows = Array.from(tbody.rows);
    const dataRows = rows.slice(0, -1); // last row may be totals
    const lastRow = rows[rows.length - 1];
    if (!lastRow) return;
    const lastFirstCell = lastRow.cells[0] ? lastRow.cells[0].textContent.toLowerCase() : '';
    if (lastFirstCell.includes('total') || lastFirstCell.includes('suma') || lastFirstCell === '') {
      const sum = dataRows.reduce((acc, row) => {
        const cell = row.cells[colIdx];
        if (!cell) return acc;
        const v = parseFloat(cell.textContent.replace(',', '.'));
        return acc + (isNaN(v) ? 0 : v);
      }, 0);
      if (lastRow.cells[colIdx]) {
        lastRow.cells[colIdx].textContent = Number.isInteger(sum) ? sum : sum.toFixed(2);
      }
    }
  });
  // Recalculate % columns if present (column name contains '%')
  const pctCols = cols.filter(c => c.includes('%') || c.toLowerCase().includes('pct'));
  pctCols.forEach(colName => {
    const pctIdx = cols.indexOf(colName);
    if (pctIdx < 0) return;
    const rows = Array.from(tbody.rows);
    const total = rows.reduce((acc, row) => {
      const cell = row.cells[pctIdx - 1]; // assume previous column is the count
      if (!cell) return acc;
      const v = parseFloat(cell.textContent.replace(',', '.'));
      return acc + (isNaN(v) ? 0 : v);
    }, 0);
    if (total > 0) {
      rows.forEach(row => {
        const countCell = row.cells[pctIdx - 1];
        const pctCell = row.cells[pctIdx];
        if (!countCell || !pctCell) return;
        const v = parseFloat(countCell.textContent.replace(',', '.'));
        if (!isNaN(v)) pctCell.textContent = ((v / total) * 100).toFixed(1) + '%';
      });
    }
  });
}

function execFmt(cmd) {
  if (!_editMode) { alert('Activa primero el modo edición.'); return; }
  document.execCommand(cmd, false, null);
}

async function saveEdits() {
  const sections = {};
  document.querySelectorAll('.doc-editable').forEach(el => {
    sections[el.dataset.section] = el.innerHTML;
  });
  const tables = {};
  document.querySelectorAll('.doc-editable-table').forEach(wrap => {
    const tid = wrap.dataset.tableId;
    if (!tid) return;
    const thead = wrap.querySelector('thead tr');
    const cols = thead ? Array.from(thead.cells).map(th => th.textContent.trim()).filter(c => c !== '') : [];
    const rows = [];
    wrap.querySelectorAll('tbody tr').forEach(tr => {
      const row = [];
      Array.from(tr.cells).forEach((td, i) => {
        if (i < cols.length) row.push(td.textContent.trim());
      });
      rows.push(row);
    });
    tables[tid] = {columns: cols, rows};
  });
  const r = await fetch('/step/ipmvp/save-edits', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({sections, tables})
  });
  const d = await r.json();
  const st = document.getElementById('edit-status');
  if (d.ok) {
    st.textContent = '✓ Guardado ' + new Date().toLocaleTimeString();
    st.style.color = '#FF8C42';
  } else {
    st.textContent = 'Error al guardar';
    st.style.color = '#fca5a5';
  }
}

async function downloadDocx() {
  const st = document.getElementById('edit-status');
  st.textContent = 'Generando Word…';
  const r = await fetch('/step/ipmvp/download/docx');
  if (!r.ok) { st.textContent = 'Error al generar Word'; return; }
  const blob = await r.blob();
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = 'informe_ipmvp.docx';
  a.click();
  URL.revokeObjectURL(url);
  st.textContent = '✓ Word descargado';
}
</script>"""

    return DOC_CSS + toolbar_html + f"""
<div style="background:#f3f4f6;min-height:100vh;padding:24px 16px;">
  <div class="doc-page" id="doc-content">
    {header_html}
    {inferidos_html}
    {traz_html}
    {sections_html}
    {tables_early}
    <section id="section-graficas" style="margin-bottom:2rem;">
      <h2 class="doc-section-title">6. Gráficas y exploración preliminar</h2>
      {charts_html}
    </section>
    {tables_late}
    {human_html}
    <section id="section-limitaciones" style="margin-bottom:2rem;">
      <h2 class="doc-section-title">Limitaciones del análisis</h2>
      <ul class="doc-list">{lims_items}</ul>
    </section>
    {conclusion_html}
    {footer_html}
  </div>
</div>"""


def build_charts_raw(
    df: pd.DataFrame,
    y_col: str,
    x_cols: List[str],
    date_col: Optional[str],
) -> List[Dict]:
    """Public wrapper — returns the 8 exploratory Plotly charts on raw (unfiltered) data."""
    all_cols = [y_col] + [c for c in x_cols if c in df.columns]
    df_num = df[all_cols].apply(pd.to_numeric, errors="coerce")
    return _build_charts(df, df_num, y_col, x_cols, date_col)
