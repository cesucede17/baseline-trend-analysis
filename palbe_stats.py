"""
palbe_stats.py — Statistical analysis module for PALBE v4
Adapted from PAYO.ipynb: correlations, scatter, histograms, OLS diagnostics
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import scipy.stats as stats

try:
    import statsmodels.api as sm
    from statsmodels.stats.diagnostic import het_breuschpagan
    _SM_AVAILABLE = True
except ImportError:
    _SM_AVAILABLE = False

try:
    from sklearn.linear_model import LinearRegression
    from sklearn.model_selection import KFold, cross_val_score
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import r2_score, mean_squared_error
    _SK_AVAILABLE = True
except ImportError:
    _SK_AVAILABLE = False


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PearsonRow:
    variable: str
    r: float
    p_value: float
    n: int
    significance: str  # "***" | "**" | "*" | "ns"


@dataclass
class OLSDiagResult:
    r2_train: float
    r2_test: float
    rmse_train: float
    rmse_test: float
    cv_r2_mean: float
    cv_r2_std: float
    coefficients: Dict[str, float]
    p_values: Dict[str, float]
    f_stat: float
    f_pvalue: float
    durbin_watson: float
    bp_stat: float
    bp_pvalue: float
    bp_interpretation: str
    fig_rvf_html: str
    fig_qq_html: str
    condition_number: float
    n_obs: int
    n_predictors: int


# ---------------------------------------------------------------------------
# 1. Pearson Ranking
# ---------------------------------------------------------------------------

def pearson_ranking(
    df: pd.DataFrame, y: str, x_cols: List[str]
) -> List[PearsonRow]:
    """Compute Pearson r and p-value between Y and each X."""
    results = []
    y_vals = pd.to_numeric(df[y], errors="coerce").dropna()

    for col in x_cols:
        if col not in df.columns or col == y:
            continue
        x_vals = pd.to_numeric(df[col], errors="coerce")
        common = y_vals.index.intersection(x_vals.dropna().index)
        if len(common) < 3:
            continue
        r, p = stats.pearsonr(y_vals.loc[common], x_vals.loc[common])
        n = len(common)
        sig = "***" if p < 0.001 else ("**" if p < 0.01 else ("*" if p < 0.05 else "ns"))
        results.append(PearsonRow(variable=col, r=round(r, 4), p_value=round(p, 6), n=n, significance=sig))

    results.sort(key=lambda row: abs(row.r), reverse=True)
    return results


# ---------------------------------------------------------------------------
# 2. Correlation Heatmap (Plotly HTML)
# ---------------------------------------------------------------------------

def corr_heatmap_html(df: pd.DataFrame, cols: List[str]) -> str:
    """Generate Plotly correlation heatmap as self-contained HTML."""
    numeric_cols = [c for c in cols if c in df.columns]
    sub = df[numeric_cols].apply(pd.to_numeric, errors="coerce").dropna()
    if sub.shape[1] < 2:
        return "<p>Datos insuficientes para heatmap.</p>"

    corr = sub.corr(method="pearson")
    labels = list(corr.columns)

    fig = go.Figure(data=go.Heatmap(
        z=corr.values,
        x=labels,
        y=labels,
        colorscale="RdBu",
        zmid=0,
        text=[[f"{v:.2f}" for v in row] for row in corr.values],
        texttemplate="%{text}",
        textfont={"size": 11},
        colorbar=dict(title="r"),
    ))
    fig.update_layout(
        title="Matriz de Correlación de Pearson",
        height=max(400, len(labels) * 60),
        margin=dict(l=10, r=10, t=40, b=10),
        font=dict(size=12),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


# ---------------------------------------------------------------------------
# 3. Scatter plot with OLS trendline (Plotly HTML)
# ---------------------------------------------------------------------------

def scatter_html(
    df: pd.DataFrame,
    y: str,
    x: str,
    color: str = "#16a34a",
    date_col: Optional[str] = None,
) -> str:
    """Generate scatter plot with OLS regression line."""
    if x not in df.columns or y not in df.columns:
        return "<p>Columnas no encontradas.</p>"
    sub = df[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
    if len(sub) < 3:
        return "<p>Datos insuficientes para scatter.</p>"

    r, p = stats.pearsonr(sub[x], sub[y])

    # Trendline
    m, b = np.polyfit(sub[x], sub[y], 1)
    x_line = np.linspace(sub[x].min(), sub[x].max(), 100)
    y_line = m * x_line + b

    hover_text = None
    if date_col and date_col in df.columns:
        hover_text = df.loc[sub.index, date_col].astype(str).tolist()

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=sub[x], y=sub[y], mode="markers",
        marker=dict(color=color, size=6, opacity=0.7),
        name="Datos",
        text=hover_text,
        hovertemplate="%{text}<br>X: %{x:.3f}<br>Y: %{y:.3f}<extra></extra>" if hover_text else None,
    ))
    fig.add_trace(go.Scatter(
        x=x_line, y=y_line, mode="lines",
        line=dict(color="#dc2626", dash="dash", width=2),
        name=f"OLS (r={r:.3f}, p={p:.4f})",
    ))
    fig.update_layout(
        title=f"{y} vs {x}  |  r = {r:.3f}  (p = {p:.4f})",
        xaxis_title=x, yaxis_title=y,
        height=420,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(x=0.01, y=0.99),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


# ---------------------------------------------------------------------------
# 4. Histogram (Plotly HTML)
# ---------------------------------------------------------------------------

def hist_html(df: pd.DataFrame, col: str, nbins: int = 40) -> str:
    """Generate histogram with KDE overlay."""
    if col not in df.columns:
        return "<p>Columna no encontrada.</p>"
    vals = pd.to_numeric(df[col], errors="coerce").dropna()
    if len(vals) < 2:
        return "<p>Datos insuficientes para histograma.</p>"

    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=vals,
        nbinsx=nbins,
        histnorm="probability density",
        marker_color="#16a34a",
        opacity=0.7,
        name="Histograma",
    ))

    # KDE
    try:
        kde = stats.gaussian_kde(vals)
        x_range = np.linspace(vals.min(), vals.max(), 200)
        fig.add_trace(go.Scatter(
            x=x_range, y=kde(x_range),
            mode="lines", line=dict(color="#dc2626", width=2),
            name="KDE",
        ))
    except Exception:
        pass

    # Vertical lines for mean and median
    fig.add_vline(x=float(vals.mean()), line_dash="dash", line_color="#2563eb",
                  annotation_text=f"Media: {vals.mean():.2f}", annotation_position="top right")
    fig.add_vline(x=float(vals.median()), line_dash="dot", line_color="#7c3aed",
                  annotation_text=f"Mediana: {vals.median():.2f}", annotation_position="top left")

    fig.update_layout(
        title=f"Distribución de {col}",
        xaxis_title=col, yaxis_title="Densidad",
        height=380,
        margin=dict(l=10, r=10, t=40, b=10),
        bargap=0.02,
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


# ---------------------------------------------------------------------------
# 5. OLS Diagnostics
# ---------------------------------------------------------------------------

def ols_diagnostics(
    df: pd.DataFrame,
    y: str,
    x_cols: List[str],
    standardize: bool = False,
    add_intercept: bool = True,
    test_size: float = 0.2,
    kfolds: int = 5,
) -> OLSDiagResult:
    """Full OLS regression with diagnostics (residuals, Q-Q, Breusch-Pagan)."""
    cols = [y] + [c for c in x_cols if c in df.columns and c != y]
    sub = df[cols].apply(pd.to_numeric, errors="coerce").dropna()

    if len(sub) < 10:
        return _empty_diag_result("Datos insuficientes (< 10 filas limpias)")

    Y = sub[y].values
    X = sub[[c for c in x_cols if c in sub.columns]].values
    feature_names = [c for c in x_cols if c in sub.columns]

    if standardize:
        scaler_y = StandardScaler()
        scaler_x = StandardScaler()
        Y = scaler_y.fit_transform(Y.reshape(-1, 1)).ravel()
        X = scaler_x.fit_transform(X)

    # Train/test split (time-ordered)
    n_test = max(1, int(len(sub) * test_size))
    X_train, X_test = X[:-n_test], X[-n_test:]
    Y_train, Y_test = Y[:-n_test], Y[-n_test:]

    # statsmodels OLS
    X_sm_train = sm.add_constant(X_train) if add_intercept else X_train
    X_sm_test = sm.add_constant(X_test, has_constant="add") if add_intercept else X_test

    try:
        ols_model = sm.OLS(Y_train, X_sm_train).fit()
        Y_pred_train = ols_model.predict(X_sm_train)
        Y_pred_test = ols_model.predict(X_sm_test)

        r2_train = float(r2_score(Y_train, Y_pred_train))
        r2_test = float(r2_score(Y_test, Y_pred_test))
        rmse_train = float(np.sqrt(mean_squared_error(Y_train, Y_pred_train)))
        rmse_test = float(np.sqrt(mean_squared_error(Y_test, Y_pred_test)))

        # Coefficients
        param_names = (["const"] if add_intercept else []) + feature_names
        coefs = {n: round(float(v), 6) for n, v in zip(param_names, ols_model.params)}
        pvals = {n: round(float(v), 6) for n, v in zip(param_names, ols_model.pvalues)}

        f_stat = float(ols_model.fvalue) if hasattr(ols_model, "fvalue") and ols_model.fvalue is not None else 0.0
        f_pval = float(ols_model.f_pvalue) if hasattr(ols_model, "f_pvalue") and ols_model.f_pvalue is not None else 1.0
        dw = float(ols_model.durbin_watson) if hasattr(ols_model, "durbin_watson") else 2.0
        cond_num = float(ols_model.condition_number) if hasattr(ols_model, "condition_number") else 0.0

        # Breusch-Pagan
        resids = ols_model.resid
        try:
            bp_lm, bp_p, _, _ = het_breuschpagan(resids, X_sm_train)
            bp_stat = float(bp_lm)
            bp_pval = float(bp_p)
            bp_interp = (
                f"Heterocedasticidad detectada (p={bp_pval:.4f} < 0.05). "
                "Considera transformar Y o usar errores robustos."
                if bp_pval < 0.05
                else f"Homocedasticidad OK (p={bp_pval:.4f} ≥ 0.05)."
            )
        except Exception:
            bp_stat, bp_pval, bp_interp = 0.0, 1.0, "Test BP no disponible."

    except Exception as exc:
        return _empty_diag_result(f"Error en OLS: {exc}")

    # K-fold CV
    cv_scores = []
    if _SK_AVAILABLE and len(X) >= kfolds * 2:
        sk_model = LinearRegression(fit_intercept=add_intercept)
        kf = KFold(n_splits=kfolds, shuffle=False)
        try:
            cv_scores = cross_val_score(sk_model, X, Y, cv=kf, scoring="r2")
        except Exception:
            cv_scores = []

    cv_mean = float(np.mean(cv_scores)) if len(cv_scores) > 0 else 0.0
    cv_std = float(np.std(cv_scores)) if len(cv_scores) > 0 else 0.0

    # Diagnostic plots
    fig_rvf_html = _plot_residuals_vs_fitted(Y_pred_train, resids)
    fig_qq_html = _plot_qq(resids)

    return OLSDiagResult(
        r2_train=r2_train,
        r2_test=r2_test,
        rmse_train=rmse_train,
        rmse_test=rmse_test,
        cv_r2_mean=cv_mean,
        cv_r2_std=cv_std,
        coefficients=coefs,
        p_values=pvals,
        f_stat=f_stat,
        f_pvalue=f_pval,
        durbin_watson=dw,
        bp_stat=bp_stat,
        bp_pvalue=bp_pval,
        bp_interpretation=bp_interp,
        fig_rvf_html=fig_rvf_html,
        fig_qq_html=fig_qq_html,
        condition_number=cond_num,
        n_obs=len(sub),
        n_predictors=len(feature_names),
    )


def _plot_residuals_vs_fitted(y_fitted: np.ndarray, residuals: np.ndarray) -> str:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=y_fitted, y=residuals, mode="markers",
        marker=dict(color="#16a34a", size=5, opacity=0.7),
        name="Residuos",
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="#dc2626")
    fig.update_layout(
        title="Residuos vs Valores Ajustados",
        xaxis_title="Valores Ajustados", yaxis_title="Residuos",
        height=350, margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _plot_qq(residuals: np.ndarray) -> str:
    (osm, osr), (slope, intercept, r) = stats.probplot(residuals, dist="norm")
    line_x = np.array([osm[0], osm[-1]])
    line_y = slope * line_x + intercept

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=osm, y=osr, mode="markers",
        marker=dict(color="#16a34a", size=5, opacity=0.7),
        name="Cuantiles",
    ))
    fig.add_trace(go.Scatter(
        x=line_x, y=line_y, mode="lines",
        line=dict(color="#dc2626", dash="dash"),
        name="Línea normal",
    ))
    fig.update_layout(
        title="Q-Q Plot (Normalidad de Residuos)",
        xaxis_title="Cuantiles Teóricos", yaxis_title="Cuantiles de Muestra",
        height=350, margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _empty_diag_result(msg: str) -> OLSDiagResult:
    return OLSDiagResult(
        r2_train=0, r2_test=0, rmse_train=0, rmse_test=0,
        cv_r2_mean=0, cv_r2_std=0,
        coefficients={}, p_values={},
        f_stat=0, f_pvalue=1, durbin_watson=2,
        bp_stat=0, bp_pvalue=1, bp_interpretation=msg,
        fig_rvf_html=f"<p>{msg}</p>", fig_qq_html=f"<p>{msg}</p>",
        condition_number=0, n_obs=0, n_predictors=0,
    )
