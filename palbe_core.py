# palbe_core.py
from __future__ import annotations

import os
import json
import datetime as dt
import re
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any, Tuple, Literal, cast

import numpy as np
import pandas as pd
import joblib
import pickle

from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.linear_model import Ridge, Lasso

import statsmodels.api as sm
from statsmodels.gam.api import GLMGam, BSplines
from statsmodels.genmod.families import Gaussian

try:
    from lightgbm import LGBMRegressor
except Exception:
    LGBMRegressor = None

try:
    from xgboost import XGBRegressor
except Exception:
    XGBRegressor = None

try:
    from interpret.glassbox import ExplainableBoostingRegressor
except Exception:
    ExplainableBoostingRegressor = None

try:
    from bartpy.sklearnmodel import SklearnModel as BARTRegressor
except Exception:
    BARTRegressor = None

# Matplotlib (modo no interactivo)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Plotly para HTML interactivo
import plotly.graph_objects as go
import plotly.io as pio

ModelType = Literal["OLS", "RF", "LGBM", "XGB", "RIDGE", "LASSO", "GAM", "EBM", "BART"]
R2NoInterceptMode = Literal["centered", "uncentered"]
DataGranularity = Literal["hourly", "daily", "weekly", "monthly"]
GranularityMode = Literal["auto", "hourly", "daily", "weekly", "monthly"]
SUPPORTED_MODEL_TYPES = {"OLS", "RF", "LGBM", "XGB", "RIDGE", "LASSO", "GAM", "EBM", "BART"}
LINEAR_MODEL_TYPES = {"OLS", "RIDGE", "LASSO", "GAM"}
SUPPORTED_DATA_GRANULARITIES = {"hourly", "daily", "weekly", "monthly"}


# =============================================================================
# Excel helpers
# =============================================================================
def _ensure_excel_exists(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Excel no encontrado: {path}")


def list_sheets(excel_path: str) -> list[str]:
    _ensure_excel_exists(excel_path)
    xls = pd.ExcelFile(excel_path)
    return list(xls.sheet_names)


def list_columns(excel_path: str, sheet: str) -> list[str]:
    _ensure_excel_exists(excel_path)
    df = pd.read_excel(excel_path, sheet_name=sheet, nrows=1)
    return list(df.columns)


def date_bounds(excel_path: str, sheet: str, date_col: str) -> dict:
    _ensure_excel_exists(excel_path)
    df = pd.read_excel(excel_path, sheet_name=sheet, usecols=[date_col])
    d = pd.to_datetime(df[date_col], errors="coerce").dropna()
    if d.empty:
        return {"ok": False, "error": "No se han detectado fechas válidas en esa columna."}
    return {
        "ok": True,
        "date_min": d.min().date().isoformat(),
        "date_max": d.max().date().isoformat(),
    }


# =============================================================================
# Config / Result
# =============================================================================
@dataclass
class HourRule:
    mode: Literal["include", "exclude"]
    h0: int
    h1: int


@dataclass
class TrainConfig:
    excel_path: str
    sheet: str

    date_col: str = ""
    target_col: str = ""
    feature_cols: List[str] = field(default_factory=list)

    date_start: str = "1900-01-01"
    date_end: str = "2100-01-01"
    include_ranges: List[Tuple[str, str]] = field(default_factory=list)
    exclude_dates: List[str] = field(default_factory=list)
    hour_rules: List[HourRule] = field(default_factory=list)
    granularity_mode: GranularityMode = "auto"

    model_type: ModelType = "RF"

    # OLS
    include_intercept: bool = True
    ols_r2_no_intercept_mode: R2NoInterceptMode = "centered"
    auto_elimination: bool = False
    t_threshold: float = 2.0
    max_iter: int = 100

    # Particion train/test (modelos no-OLS)
    test_size: float = 0.2
    random_state: int = 42

    # RF
    rf_n_estimators: int = 400
    rf_max_depth: Optional[int] = None
    rf_min_samples_leaf: int = 1

    # LightGBM
    lgbm_n_estimators: int = 400
    lgbm_learning_rate: float = 0.05
    lgbm_max_depth: int = -1
    lgbm_num_leaves: int = 31
    lgbm_min_child_samples: int = 20

    # XGBoost
    xgb_n_estimators: int = 500
    xgb_learning_rate: float = 0.05
    xgb_max_depth: int = 6
    xgb_subsample: float = 1.0
    xgb_colsample_bytree: float = 1.0

    # Ridge / Lasso
    ridge_alpha: float = 1.0
    lasso_alpha: float = 0.01
    lasso_max_iter: int = 5000

    # GAM
    gam_n_splines: int = 8
    gam_spline_degree: int = 3
    gam_alpha: float = 0.1

    # EBM
    ebm_max_bins: int = 256
    ebm_interactions: int = 8
    ebm_learning_rate: float = 0.03
    ebm_max_rounds: int = 500

    # BART (si no hay libreria especifica, se usa aproximacion)
    bart_num_trees: int = 200
    bart_learning_rate: float = 0.05
    bart_max_depth: int = 2

    out_dir: str = "outputs"
    model_name: str = "palbe_model"


@dataclass
class RunResult:
    ok: bool
    message: str
    model_type: Optional[str] = None
    used_features: List[str] = field(default_factory=list)
    dropped_features: List[str] = field(default_factory=list)
    n_total: int = 0
    n_used: int = 0
    metrics: Dict[str, float] = field(default_factory=dict)
    artifacts: Dict[str, str] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Internal utilities
# =============================================================================
def _to_numeric_df(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    X = df[cols].copy()
    for c in X.columns:
        X[c] = pd.to_numeric(X[c], errors="coerce")
    X = X.replace([np.inf, -np.inf], np.nan)
    return X


def _drop_all_nan_cols(X: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    all_nan = [c for c in X.columns if X[c].isna().all()]
    if all_nan:
        X = X.drop(columns=all_nan)
    return X, all_nan


def _normalize_r2_no_intercept_mode(mode: str) -> R2NoInterceptMode:
    return "uncentered" if str(mode).strip().lower() == "uncentered" else "centered"


def _normalize_granularity_mode(mode: str) -> GranularityMode:
    m = str(mode or "auto").strip().lower()
    if m in SUPPORTED_DATA_GRANULARITIES:
        return cast(GranularityMode, m)
    return "auto"


def _infer_data_granularity(fechas: pd.Series) -> DataGranularity:
    d = pd.to_datetime(fechas, errors="coerce").dropna().sort_values()
    if d.empty:
        return "daily"

    subday = (
        (d.dt.hour != 0)
        | (d.dt.minute != 0)
        | (d.dt.second != 0)
        | (d.dt.microsecond != 0)
    )
    try:
        subday = subday | (d.dt.nanosecond != 0)
    except Exception:
        pass
    has_subday = bool(subday.any())
    max_obs_per_day = int(d.dt.normalize().value_counts().max()) if len(d) else 0

    d_unique = pd.Series(d.unique()).sort_values()
    if has_subday and len(d_unique) >= 2:
        dt_hours = d_unique.diff().dropna().dt.total_seconds() / 3600.0
        if not dt_hours.empty:
            med_hours = float(dt_hours.median())
            if med_hours < 24.0 or max_obs_per_day > 1:
                return "hourly"

    d_days = d.dt.normalize().drop_duplicates().sort_values()
    if len(d_days) < 2:
        return "hourly" if has_subday else "daily"

    dt_days = d_days.diff().dropna().dt.total_seconds() / 86400.0
    if dt_days.empty:
        return "hourly" if has_subday else "daily"

    share_week = float(((dt_days >= 6.0) & (dt_days <= 8.0)).mean())
    share_month = float(((dt_days >= 27.0) & (dt_days <= 32.0)).mean())
    med_days = float(dt_days.median())

    if share_month >= 0.6 or med_days >= 27.0:
        return "monthly"
    if share_week >= 0.6 or (6.0 <= med_days <= 8.0):
        return "weekly"
    if has_subday:
        return "hourly"
    return "daily"


def _resolve_granularity(
    fechas: pd.Series,
    granularity_mode: str,
) -> Tuple[DataGranularity, DataGranularity, GranularityMode]:
    mode = _normalize_granularity_mode(granularity_mode)
    detected = _infer_data_granularity(fechas)
    if mode == "auto":
        return detected, detected, mode
    return detected, cast(DataGranularity, mode), mode


def _ashrae_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    p: int,
    r2_mode: R2NoInterceptMode = "centered",
) -> Dict[str, float]:
    """
    Métricas tipo ASHRAE/IPMVP.
    Compatible con scikit-learn antiguo (evita squared=False).
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    n = int(len(y_true))
    if n == 0:
        return {"R2": np.nan, "RMSE": np.nan, "MAE": np.nan, "CVRMSE": np.nan, "NMBE": np.nan}

    mse = float(mean_squared_error(y_true, y_pred))
    rmse = float(np.sqrt(mse))
    mae = float(mean_absolute_error(y_true, y_pred))
    r2_mode = _normalize_r2_no_intercept_mode(r2_mode)
    if n > 1:
        if r2_mode == "uncentered":
            # R2 no centrado: baseline = 0
            ss_res = float(np.sum((y_true - y_pred) ** 2))
            ss_tot0 = float(np.sum(y_true ** 2))
            r2 = float(1.0 - (ss_res / ss_tot0)) if ss_tot0 != 0 else np.nan
        else:
            # R2 centrado: baseline = media(y)
            r2 = float(r2_score(y_true, y_pred))
    else:
        r2 = np.nan

    mean_y = float(np.mean(y_true)) if n else np.nan
    cvrmse = float((rmse / mean_y) * 100.0) if mean_y and mean_y != 0 else np.nan

    denom = (n - int(p)) * mean_y if (n - int(p)) > 0 and mean_y not in (0, None) else np.nan
    nmbe = float((np.sum(y_true - y_pred) / denom) * 100.0) if denom == denom else np.nan

    return {"R2": r2, "RMSE": rmse, "MAE": mae, "CVRMSE": cvrmse, "NMBE": nmbe}


def _build_date_mask(fechas: pd.Series, cfg: TrainConfig) -> pd.Series:
    fechas = pd.to_datetime(fechas, errors="coerce")
    fdates = fechas.dt.date

    a_d = pd.to_datetime(cfg.date_start).date()
    b_d = pd.to_datetime(cfg.date_end).date()
    if a_d > b_d:
        a_d, b_d = b_d, a_d
    mask = (fdates >= a_d) & (fdates <= b_d)

    if cfg.include_ranges:
        mask = pd.Series(False, index=fechas.index)
        for a, b in cfg.include_ranges:
            aa = pd.to_datetime(a).date()
            bb = pd.to_datetime(b).date()
            if aa > bb:
                aa, bb = bb, aa
            mask |= (fdates >= aa) & (fdates <= bb)

    if cfg.exclude_dates:
        # pd.to_datetime on a list returns DatetimeIndex (no .dt); wrap in Series so .dt.date works.
        ex = set(pd.to_datetime(pd.Series(cfg.exclude_dates), errors="coerce").dropna().dt.date)
        mask &= ~fdates.isin(ex)

    mask &= ~fechas.isna()
    return mask


def _apply_hour_rules(mask: pd.Series, fechas: pd.Series, rules: List[HourRule]) -> pd.Series:
    if not rules:
        return mask
    fechas = pd.to_datetime(fechas, errors="coerce")
    horas = fechas.dt.hour

    include_union = None
    exclude_union = None

    for r in rules:
        h0, h1 = int(r.h0), int(r.h1)
        if not (0 <= h0 <= 23 and 0 <= h1 <= 23):
            continue
        if h0 <= h1:
            dentro = horas.between(h0, h1)
        else:
            dentro = horas.between(h0, 23) | horas.between(0, h1)

        if r.mode == "include":
            include_union = dentro if include_union is None else (include_union | dentro)
        else:
            exclude_union = dentro if exclude_union is None else (exclude_union | dentro)

    out = mask.copy()
    if include_union is not None:
        out &= include_union
    if exclude_union is not None:
        out &= ~exclude_union
    return out


def _clean_xy(df: pd.DataFrame, date_col: str, y_col: str, X_cols: List[str]):
    df2 = df.copy()
    df2[date_col] = pd.to_datetime(df2[date_col], errors="coerce")

    y = pd.to_numeric(df2[y_col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    X = _to_numeric_df(df2, X_cols)
    X, all_nan_cols = _drop_all_nan_cols(X)

    keep = ~df2[date_col].isna() & ~y.isna()
    for c in X.columns:
        keep &= ~X[c].isna()

    df_clean = df2.loc[keep].copy()
    return df_clean, y.loc[keep].astype(float), X.loc[keep].astype(float), all_nan_cols


def _backward_elimination_by_t(
    X_in: pd.DataFrame,
    y_in: pd.Series,
    include_const: bool,
    t_threshold: float,
    max_iter: int
):
    X_curr = X_in.copy()
    kept = list(X_curr.columns)
    dropped: List[str] = []

    for _ in range(max_iter):
        if X_curr.shape[1] == 0:
            break
        X_ols = sm.add_constant(X_curr, has_constant="add") if include_const else X_curr
        model = sm.OLS(y_in.astype(float), X_ols.astype(float)).fit()

        tvals = model.tvalues.copy()
        if include_const and "const" in tvals.index:
            tvals = tvals.drop("const")

        candidates = tvals[abs(tvals) < float(t_threshold)]
        if candidates.empty:
            return X_curr, kept, dropped, model

        worst = candidates.abs().idxmin()
        dropped.append(worst)
        kept.remove(worst)
        X_curr = X_curr.drop(columns=[worst])

    X_ols = sm.add_constant(X_curr, has_constant="add") if include_const else X_curr
    model = sm.OLS(y_in.astype(float), X_ols.astype(float)).fit() if X_curr.shape[1] > 0 else None
    return X_curr, kept, dropped, model


def _normalize_model_type(model_type: str) -> ModelType:
    mt = (model_type or "RF").strip().upper()
    if mt not in SUPPORTED_MODEL_TYPES:
        mt = "RF"
    return cast(ModelType, mt)


def _safe_test_size(test_size: float) -> float:
    ts = float(test_size)
    if ts <= 0 or ts >= 0.95:
        return 0.2
    return ts


def _model_label(model_type: ModelType) -> str:
    labels = {
        "OLS": "OLS",
        "RF": "Random Forest",
        "LGBM": "LightGBM",
        "XGB": "XGBoost",
        "RIDGE": "Ridge",
        "LASSO": "Lasso",
        "GAM": "GAM",
        "EBM": "Explainable Boosting Machine",
        "BART": "BART",
    }
    return labels.get(model_type, str(model_type))


def _model_suffix(model_type: ModelType) -> str:
    suffix = {
        "OLS": "ols",
        "RF": "rf",
        "LGBM": "lgbm",
        "XGB": "xgb",
        "RIDGE": "ridge",
        "LASSO": "lasso",
        "GAM": "gam",
        "EBM": "ebm",
        "BART": "bart",
    }
    return suffix.get(model_type, str(model_type).lower())


def _required_feature_names(model, model_type: ModelType, fallback_cols: List[str]) -> List[str]:
    if model_type == "OLS":
        exog_names = list(getattr(getattr(model, "model", None), "exog_names", []) or [])
        names = [c for c in exog_names if c != "const"]
        return names or fallback_cols

    if model_type == "GAM":
        smoother = getattr(getattr(model, "model", None), "smoother", None)
        names = list(getattr(smoother, "variable_names", []) or [])
        return names or fallback_cols

    names_in = getattr(model, "feature_names_in_", None)
    if names_in is not None:
        names = [str(c) for c in names_in]
        if names:
            return names
    return fallback_cols


def _align_X_columns(X: pd.DataFrame, required_cols: List[str], where: str) -> pd.DataFrame:
    missing = [c for c in required_cols if c not in X.columns]
    if not missing:
        return X.loc[:, required_cols].astype(float)

    # Alias robusto para modelos que sanitizan nombres (ej. LightGBM: espacios -> "_")
    def _alias_name(s: Any) -> str:
        return re.sub(r"\s+", "_", str(s).strip())

    alias_to_real: Dict[str, str] = {}
    for col in X.columns:
        alias_to_real[_alias_name(col)] = col

    can_alias = all(c in alias_to_real for c in missing)
    if can_alias:
        resolved = [alias_to_real.get(c, c) for c in required_cols]
        Xa = X.loc[:, resolved].copy()
        Xa.columns = required_cols
        return Xa.astype(float)

    raise ValueError(f"Faltan columnas requeridas en {where}: {missing}")


def _extract_feature_scores(model, model_type: ModelType, feature_cols: List[str]) -> Dict[str, float]:
    if hasattr(model, "feature_importances_"):
        vals = np.asarray(getattr(model, "feature_importances_"), dtype=float).reshape(-1)
        if len(vals) == len(feature_cols):
            return {c: float(v) for c, v in zip(feature_cols, vals)}

    if hasattr(model, "coef_"):
        vals = np.asarray(getattr(model, "coef_"), dtype=float).reshape(-1)
        if len(vals) == len(feature_cols):
            return {c: float(v) for c, v in zip(feature_cols, vals)}

    if hasattr(model, "term_importances"):
        try:
            vals = np.asarray(model.term_importances(), dtype=float).reshape(-1)
            if len(vals) >= len(feature_cols):
                return {c: float(v) for c, v in zip(feature_cols, vals[: len(feature_cols)])}
        except Exception:
            pass

    if model_type == "OLS" and hasattr(model, "params"):
        params = model.params.copy()
        params = params.drop("const") if "const" in params.index else params
        return {str(k): float(v) for k, v in params.to_dict().items()}

    return {}


def _format_formula_number(value: float, precision: int = 8) -> str:
    txt = f"{float(value):.{precision}g}"
    return "0" if txt in {"-0", "-0.0"} else txt


def _quote_formula_name(name: Any) -> str:
    n = str(name)
    if re.fullmatch(r"[A-Za-z_]\w*", n):
        return n
    return f'"{n.replace("\"", "\\\"")}"'


def _build_ols_formula(model, target_name: str, include_intercept: bool, precision: int = 8) -> str:
    params = getattr(model, "params", None)
    if params is None:
        return ""

    rhs_terms: List[Tuple[float, Optional[str]]] = []

    if include_intercept and "const" in params.index:
        coef = float(params["const"])
        if np.isfinite(coef):
            rhs_terms.append((coef, None))

    params_no_const = params.drop("const") if "const" in params.index else params
    for term_name, coef in params_no_const.items():
        coef_f = float(coef)
        if not np.isfinite(coef_f):
            continue
        rhs_terms.append((coef_f, _quote_formula_name(term_name)))

    if not rhs_terms:
        return f"{_quote_formula_name(target_name)} = 0"

    rhs_parts: List[str] = []
    for i, (coef, term_name) in enumerate(rhs_terms):
        coef_txt = _format_formula_number(abs(coef), precision=precision)
        term_expr = coef_txt if term_name is None else f"{coef_txt}*{term_name}"
        if i == 0:
            rhs_parts.append(term_expr if coef >= 0 else f"-{term_expr}")
        else:
            rhs_parts.append(f"+ {term_expr}" if coef >= 0 else f"- {term_expr}")

    return f"{_quote_formula_name(target_name)} = {' '.join(rhs_parts)}"


def _extract_ols_variable_stats(model) -> List[Dict[str, Any]]:
    params = getattr(model, "params", None)
    if params is None:
        return []

    tvalues = getattr(model, "tvalues", None)
    pvalues = getattr(model, "pvalues", None)
    bse = getattr(model, "bse", None)
    conf_int = None
    try:
        conf_int = model.conf_int(alpha=0.05)
    except Exception:
        conf_int = None

    def _to_num(value: Any) -> Optional[float]:
        try:
            v = float(value)
            return v if np.isfinite(v) else None
        except Exception:
            return None

    rows: List[Dict[str, Any]] = []
    for var in params.index:
        label = "(intercepto)" if str(var) == "const" else str(var)
        ci_low = None
        ci_high = None
        if conf_int is not None:
            try:
                ci_low = _to_num(conf_int.loc[var, 0])
                ci_high = _to_num(conf_int.loc[var, 1])
            except Exception:
                pass
        rows.append(
            {
                "variable": label,
                "coef": _to_num(params.get(var, np.nan)),
                "std_err": _to_num(bse.get(var, np.nan) if bse is not None else np.nan),
                "t_value": _to_num(tvalues.get(var, np.nan) if tvalues is not None else np.nan),
                "p_value": _to_num(pvalues.get(var, np.nan) if pvalues is not None else np.nan),
                "ci95_low": ci_low,
                "ci95_high": ci_high,
            }
        )
    return rows


# =============================================================================
# Plot / export helpers
# =============================================================================
def _safe_savefig(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    plt.tight_layout()
    plt.savefig(path, dpi=140)
    plt.close()
    return path


def _plot_timeseries_plotly_html(df_out: pd.DataFrame, date_col: str, y_col: str, yhat_col: str, out_path_html: str) -> str:
    d = df_out.sort_values(date_col).copy()
    x = pd.to_datetime(d[date_col], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=d[y_col], mode="lines", name="Real"))
    fig.add_trace(go.Scatter(x=x, y=d[yhat_col], mode="lines", name="Predicho"))

    fig.update_layout(
        title="Serie temporal: Real vs Predicho",
        xaxis_title="Fecha",
        yaxis_title=y_col,
        hovermode="x unified",
        autosize=True,
        margin=dict(l=40, r=20, t=60, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )

    os.makedirs(os.path.dirname(out_path_html), exist_ok=True)
    html = pio.to_html(fig, full_html=True, include_plotlyjs="inline")
    with open(out_path_html, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path_html


def _plot_weekly_completeness_heatmap_html(
    df: pd.DataFrame,
    date_col: str,
    param_cols: List[str],
    out_path_html: str,
) -> str:
    cols = [c for c in dict.fromkeys(param_cols) if c and c in df.columns and c != date_col]
    if not cols:
        raise ValueError("No hay parametros validos para el mapa de completitud.")

    d = df[[date_col] + cols].copy()
    d[date_col] = pd.to_datetime(d[date_col], errors="coerce")
    d = d.loc[~d[date_col].isna()].copy()
    if d.empty:
        raise ValueError("No hay fechas validas para construir el mapa de completitud.")

    for c in cols:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d[cols] = d[cols].replace([np.inf, -np.inf], np.nan)
    d["__day"] = d[date_col].dt.normalize()

    rows_per_day = d.groupby("__day").size().astype(float)
    non_missing = d.groupby("__day")[cols].count().astype(float)
    missing_mask = non_missing.lt(rows_per_day, axis=0)
    missing_params_count = missing_mask.sum(axis=1).astype(float)

    n_params = len(cols)
    start_day = pd.Timestamp(rows_per_day.index.min())
    end_day = pd.Timestamp(rows_per_day.index.max())
    valid_days = pd.date_range(start=start_day, end=end_day, freq="D")
    missing_mask = missing_mask.reindex(valid_days, fill_value=True)
    missing_params_count = missing_params_count.reindex(valid_days, fill_value=float(n_params))

    first_monday = start_day - pd.Timedelta(days=int(start_day.weekday()))
    last_sunday = end_day + pd.Timedelta(days=int(6 - end_day.weekday()))
    calendar_days = pd.date_range(start=first_monday, end=last_sunday, freq="D")
    week_start_for_day = calendar_days - pd.to_timedelta(calendar_days.weekday, unit="D")
    week_starts = pd.Index(pd.unique(week_start_for_day)).sort_values()
    week_pos = {pd.Timestamp(w): i for i, w in enumerate(week_starts)}

    day_names = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]
    week_labels = [pd.Timestamp(w).strftime("%Y-%m-%d") for w in week_starts]
    z = np.full((7, len(week_starts)), np.nan, dtype=float)
    missing_params_text = np.full((7, len(week_starts)), "", dtype=object)

    for day in calendar_days:
        week_start = pd.Timestamp(day - pd.Timedelta(days=int(day.weekday())))
        col_idx = week_pos[week_start]
        row_idx = int(day.weekday())

        if day < start_day or day > end_day:
            continue
        miss = float(missing_params_count.loc[day])
        miss_cols = [c for c in cols if bool(missing_mask.at[day, c])]
        miss_cols_text = ", ".join(miss_cols) if miss_cols else "Ninguna"
        z[row_idx, col_idx] = miss
        missing_params_text[row_idx, col_idx] = miss_cols_text

    if n_params <= 10:
        tickvals = list(range(0, n_params + 1))
    else:
        tickvals = sorted(
            {
                0,
                int(round(0.25 * n_params)),
                int(round(0.50 * n_params)),
                int(round(0.75 * n_params)),
                n_params,
            }
        )

    fig = go.Figure()
    fig.add_trace(
        go.Heatmap(
            z=z,
            x=week_labels,
            y=day_names,
            zmin=0,
            zmax=max(1, n_params),
            colorscale=[
                [0.00, "#1a9850"],
                [0.35, "#fee08b"],
                [0.70, "#f46d43"],
                [1.00, "#d73027"],
            ],
            xgap=2,
            ygap=2,
            hoverongaps=False,
            customdata=missing_params_text,
            hovertemplate="Semana (inicio): %{x}<br>Dia: %{y}<br>Parametros faltantes: %{z:.0f}<br>Variables faltantes: %{customdata}<extra></extra>",
            colorbar=dict(
                title="Parametros faltantes",
                tickmode="array",
                tickvals=tickvals,
            ),
        )
    )

    for r_idx, day_name in enumerate(day_names):
        for c_idx, week_label in enumerate(week_labels):
            miss = z[r_idx, c_idx]
            if np.isnan(miss) or miss <= 0:
                continue
            fig.add_annotation(
                x=week_label,
                y=day_name,
                text=str(int(round(miss))),
                showarrow=False,
                font=dict(size=11, color="#1d1d1d"),
            )

    fig.update_layout(
        title="Mapa de calor de disponibilidad de datos",
        xaxis_title="Semana (inicio en lunes)",
        yaxis_title="Dia de la semana",
        yaxis=dict(autorange="reversed", type="category"),
        xaxis=dict(type="category"),
        margin=dict(l=60, r=40, t=65, b=50),
        autosize=True,
    )

    out_dir = os.path.dirname(out_path_html) or "."
    os.makedirs(out_dir, exist_ok=True)
    html = pio.to_html(fig, full_html=True, include_plotlyjs="inline")
    with open(out_path_html, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path_html


def _plot_scatter(y_true: np.ndarray, y_pred: np.ndarray, out_path: str) -> str:
    plt.figure(figsize=(5, 5))
    plt.scatter(y_true, y_pred, s=12)
    mn = float(np.nanmin([np.nanmin(y_true), np.nanmin(y_pred)]))
    mx = float(np.nanmax([np.nanmax(y_true), np.nanmax(y_pred)]))
    plt.plot([mn, mx], [mn, mx])
    plt.title("Dispersión: y_real vs y_pred")
    plt.xlabel("y_real")
    plt.ylabel("y_pred")
    return _safe_savefig(out_path)


def _plot_residuals(resid: np.ndarray, out_path: str) -> str:
    plt.figure(figsize=(7, 4))
    plt.hist(resid, bins=30)
    plt.title("Histograma de residuos (y_real - y_pred)")
    plt.xlabel("Residuo")
    plt.ylabel("Frecuencia")
    return _safe_savefig(out_path)


def _plot_feature_scores(
    cols: List[str],
    vals: np.ndarray,
    out_path: str,
    title: str = "Feature importances (top)",
    xlabel: str = "Score",
    topk: int = 25,
) -> str:
    s = pd.Series(vals, index=cols).sort_values(ascending=False).head(topk)
    plt.figure(figsize=(8, max(4, 0.25 * len(s))))
    plt.barh(list(reversed(s.index.tolist())), list(reversed(s.values.tolist())))
    plt.title(title)
    plt.xlabel(xlabel)
    return _safe_savefig(out_path)


def _plot_rf_importances(cols: List[str], imps: np.ndarray, out_path: str, topk: int = 25) -> str:
    return _plot_feature_scores(
        cols=cols,
        vals=imps,
        out_path=out_path,
        title="Random Forest: Feature importances (top)",
        xlabel="Importancia",
        topk=topk,
    )


def _plot_ols_coefs(model, out_path: str, topk: int = 30) -> str:
    params = model.params.copy()
    params = params.drop("const") if "const" in params.index else params
    s = params.reindex(params.abs().sort_values(ascending=False).index).head(topk)
    plt.figure(figsize=(8, max(4, 0.25 * len(s))))
    plt.barh(list(reversed(s.index.tolist())), list(reversed(s.values.tolist())))
    plt.title("OLS: Coeficientes (top)")
    plt.xlabel("Coeficiente")
    return _safe_savefig(out_path)


def _plot_qq(resid: np.ndarray, out_path: str) -> str:
    fig = sm.qqplot(resid, line="45", fit=True)
    fig.suptitle("OLS: QQ plot residuos")
    plt.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path


def _export_excel(df_out: pd.DataFrame, metrics: Dict[str, float], out_path: str) -> str:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    m = pd.DataFrame({"metric": list(metrics.keys()), "value": list(metrics.values())})
    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        m.to_excel(w, sheet_name="metrics", index=False)
        df_out.to_excel(w, sheet_name="predictions", index=False)
    return out_path


# =============================================================================
# Training
# =============================================================================
def train(cfg: TrainConfig) -> RunResult:
    try:
        _ensure_excel_exists(cfg.excel_path)
        os.makedirs(cfg.out_dir, exist_ok=True)

        df = pd.read_excel(cfg.excel_path, sheet_name=cfg.sheet)
        n_total = int(len(df))
        model_type = _normalize_model_type(cfg.model_type)
        cfg.granularity_mode = _normalize_granularity_mode(cfg.granularity_mode)

        if cfg.date_col not in df.columns:
            return RunResult(False, f"date_col '{cfg.date_col}' no existe en la hoja.", model_type=model_type)
        if cfg.target_col not in df.columns:
            return RunResult(False, f"target_col '{cfg.target_col}' no existe en la hoja.", model_type=model_type)
        if not cfg.feature_cols:
            return RunResult(False, "feature_cols vacio: selecciona al menos una X.", model_type=model_type)

        X_cols = [c for c in cfg.feature_cols if c not in (cfg.date_col, cfg.target_col)]
        if not X_cols:
            return RunResult(False, "Tras excluir fecha e y, no queda ninguna X.", model_type=model_type)

        mask = _build_date_mask(df[cfg.date_col], cfg)
        mask = _apply_hour_rules(mask, df[cfg.date_col], cfg.hour_rules)
        df_f = df.loc[mask].copy()

        df_clean, y, X, all_nan_cols = _clean_xy(df_f, cfg.date_col, cfg.target_col, X_cols)
        n_used = int(len(df_clean))
        granularity_detected, granularity_used, granularity_mode = _resolve_granularity(
            df_clean[cfg.date_col] if n_used else pd.Series(dtype="datetime64[ns]"),
            cfg.granularity_mode,
        )

        if n_used < 10:
            return RunResult(
                False,
                "Muy pocas filas tras filtros/limpieza (recomendable >=10).",
                model_type=model_type,
                n_total=n_total,
                n_used=n_used,
                extra={
                    "granularity_mode": granularity_mode,
                    "granularity_detected": granularity_detected,
                    "granularity_used": granularity_used,
                },
            )

        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(cfg.out_dir, f"{cfg.model_name}_{model_type}_{stamp}")
        artifacts: Dict[str, str] = {}
        artifacts["plot_weekly_completeness_html"] = _plot_weekly_completeness_heatmap_html(
            df_f,
            cfg.date_col,
            [cfg.target_col] + list(X_cols),
            base + "_weekly_completeness.html",
        )

        # ---------------------------------------------------------------------
        # OLS
        # ---------------------------------------------------------------------
        if model_type == "OLS":
            dropped_features: List[str] = []
            used_features: List[str] = []
            ols_r2_mode = "centered"
            if not cfg.include_intercept:
                ols_r2_mode = _normalize_r2_no_intercept_mode(cfg.ols_r2_no_intercept_mode)

            if cfg.auto_elimination:
                X_final, kept, dropped, model = _backward_elimination_by_t(
                    X, y, cfg.include_intercept, cfg.t_threshold, cfg.max_iter
                )
                used_features = kept
                dropped_features = dropped
                Xp = X_final
            else:
                X_ols = sm.add_constant(X, has_constant="add") if cfg.include_intercept else X
                model = sm.OLS(y, X_ols).fit()
                used_features = list(X.columns)
                Xp = X

            Xp_ols = sm.add_constant(Xp, has_constant="add") if cfg.include_intercept else Xp
            yhat = np.asarray(model.predict(Xp_ols.astype(float)))
            metrics = _ashrae_metrics(
                y.values,
                yhat,
                p=int(Xp_ols.shape[1]),
                r2_mode=cast(R2NoInterceptMode, ols_r2_mode),
            )
            ols_formula = _build_ols_formula(model, cfg.target_col, cfg.include_intercept)
            ols_variable_stats = _extract_ols_variable_stats(model)

            model_joblib_path = base + "_ols.joblib"
            joblib.dump(model, model_joblib_path)
            artifacts["model_joblib"] = model_joblib_path

            model_pkl_path = base + "_ols.pkl"
            with open(model_pkl_path, "wb") as f:
                pickle.dump(model, f)
            artifacts["model_pkl"] = model_pkl_path

            summary_path = base + "_ols_summary.txt"
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(str(model.summary()))
            artifacts["summary"] = summary_path

            df_out = df_clean[[cfg.date_col, cfg.target_col] + list(Xp.columns)].copy()
            df_out["y_pred"] = yhat
            df_out["resid"] = df_out[cfg.target_col].astype(float) - df_out["y_pred"].astype(float)

            artifacts["plot_timeseries_html"] = _plot_timeseries_plotly_html(
                df_out, cfg.date_col, cfg.target_col, "y_pred", base + "_ts.html"
            )
            artifacts["plot_scatter"] = _plot_scatter(
                df_out[cfg.target_col].values, df_out["y_pred"].values, base + "_scatter.png"
            )
            artifacts["plot_residuals"] = _plot_residuals(df_out["resid"].values, base + "_resid.png")
            artifacts["plot_ols_coefs"] = _plot_ols_coefs(model, base + "_coefs.png")
            artifacts["plot_ols_qq"] = _plot_qq(df_out["resid"].values, base + "_qq.png")

            excel_path = base + "_export.xlsx"
            artifacts["excel_export"] = _export_excel(df_out, metrics, excel_path)

            meta = {
                "cfg": asdict(cfg),
                "timestamp": stamp,
                "date_col_used": cfg.date_col,
                "granularity_mode": granularity_mode,
                "granularity_detected": granularity_detected,
                "granularity_used": granularity_used,
                "features_used": used_features,
                "features_dropped": dropped_features + all_nan_cols,
                "n_total_raw": n_total,
                "n_used": n_used,
                "metrics": metrics,
                "ols_formula": ols_formula,
                "ols_variable_stats": ols_variable_stats,
            }
            meta_path = base + "_meta.json"
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            artifacts["meta"] = meta_path

            return RunResult(
                ok=True,
                message="Entrenamiento OLS completado (export + graficas; principal interactiva).",
                model_type="OLS",
                used_features=used_features,
                dropped_features=dropped_features + all_nan_cols,
                n_total=n_total,
                n_used=n_used,
                metrics=metrics,
                artifacts=artifacts,
                extra={
                    "date_col_used": cfg.date_col,
                    "include_intercept": cfg.include_intercept,
                    "ols_r2_no_intercept_mode": ols_r2_mode,
                    "granularity_mode": granularity_mode,
                    "granularity_detected": granularity_detected,
                    "granularity_used": granularity_used,
                    "ols_formula": ols_formula,
                    "ols_variable_stats": ols_variable_stats,
                },
            )

        # ---------------------------------------------------------------------
        # Modelos no-OLS (train/test)
        # ---------------------------------------------------------------------
        ts = _safe_test_size(cfg.test_size)
        X_train, X_test, y_train, y_test, idx_train, idx_test = train_test_split(
            X, y, X.index, test_size=ts, random_state=int(cfg.random_state)
        )

        model = None
        y_pred: np.ndarray
        extra_note = ""

        if model_type == "RF":
            model = RandomForestRegressor(
                n_estimators=max(10, int(cfg.rf_n_estimators)),
                max_depth=cfg.rf_max_depth if cfg.rf_max_depth is None else int(cfg.rf_max_depth),
                min_samples_leaf=max(1, int(cfg.rf_min_samples_leaf)),
                random_state=int(cfg.random_state),
                n_jobs=-1,
            )
            model.fit(X_train, y_train)
            y_pred = np.asarray(model.predict(X_test))

        elif model_type == "LGBM":
            if LGBMRegressor is None:
                return RunResult(False, "LightGBM no disponible. Instala paquete 'lightgbm'.", model_type=model_type)
            model = LGBMRegressor(
                n_estimators=max(10, int(cfg.lgbm_n_estimators)),
                learning_rate=max(1e-4, float(cfg.lgbm_learning_rate)),
                max_depth=int(cfg.lgbm_max_depth),
                num_leaves=max(2, int(cfg.lgbm_num_leaves)),
                min_child_samples=max(1, int(cfg.lgbm_min_child_samples)),
                random_state=int(cfg.random_state),
                n_jobs=-1,
            )
            model.fit(X_train, y_train)
            y_pred = np.asarray(model.predict(X_test))

        elif model_type == "XGB":
            if XGBRegressor is None:
                return RunResult(False, "XGBoost no disponible. Instala paquete 'xgboost'.", model_type=model_type)
            model = XGBRegressor(
                n_estimators=max(10, int(cfg.xgb_n_estimators)),
                learning_rate=max(1e-4, float(cfg.xgb_learning_rate)),
                max_depth=max(1, int(cfg.xgb_max_depth)),
                subsample=max(0.1, min(1.0, float(cfg.xgb_subsample))),
                colsample_bytree=max(0.1, min(1.0, float(cfg.xgb_colsample_bytree))),
                random_state=int(cfg.random_state),
                objective="reg:squarederror",
                n_jobs=-1,
                verbosity=0,
            )
            model.fit(X_train, y_train)
            y_pred = np.asarray(model.predict(X_test))

        elif model_type == "RIDGE":
            model = Ridge(alpha=max(1e-8, float(cfg.ridge_alpha)), fit_intercept=bool(cfg.include_intercept))
            model.fit(X_train, y_train)
            y_pred = np.asarray(model.predict(X_test))

        elif model_type == "LASSO":
            model = Lasso(
                alpha=max(1e-8, float(cfg.lasso_alpha)),
                fit_intercept=bool(cfg.include_intercept),
                max_iter=max(100, int(cfg.lasso_max_iter)),
            )
            model.fit(X_train, y_train)
            y_pred = np.asarray(model.predict(X_test))

        elif model_type == "GAM":
            degree = max(1, int(cfg.gam_spline_degree))
            dfs: List[int] = []
            for col in X_train.columns:
                nunique = int(X_train[col].nunique(dropna=True))
                if nunique <= degree:
                    return RunResult(
                        False,
                        f"GAM requiere mayor variabilidad en '{col}' (valores unicos={nunique}, degree={degree}).",
                        model_type=model_type,
                    )
                df_col = min(max(degree + 1, int(cfg.gam_n_splines)), nunique)
                dfs.append(df_col)

            bs = BSplines(X_train, df=dfs, degree=[degree] * len(dfs))
            exog_train = sm.add_constant(X_train, has_constant="add") if cfg.include_intercept else X_train
            alpha = np.repeat(max(1e-8, float(cfg.gam_alpha)), len(dfs))
            gam = GLMGam(
                y_train.values,
                exog=exog_train.astype(float),
                smoother=bs,
                alpha=alpha,
                family=Gaussian(),
            )
            model = gam.fit()
            X_test_gam = X_test.copy()
            for col in X_train.columns:
                lo = float(X_train[col].min())
                hi = float(X_train[col].max())
                X_test_gam[col] = X_test_gam[col].clip(lower=lo, upper=hi)
            exog_test = sm.add_constant(X_test_gam, has_constant="add") if cfg.include_intercept else X_test_gam
            y_pred = np.asarray(model.predict(exog=exog_test.astype(float), exog_smooth=X_test_gam.astype(float)))

        elif model_type == "EBM":
            if ExplainableBoostingRegressor is not None:
                model = ExplainableBoostingRegressor(
                    max_bins=max(16, int(cfg.ebm_max_bins)),
                    interactions=max(0, int(cfg.ebm_interactions)),
                    learning_rate=max(1e-4, float(cfg.ebm_learning_rate)),
                    max_rounds=max(50, int(cfg.ebm_max_rounds)),
                    random_state=int(cfg.random_state),
                )
                model.fit(X_train, y_train)
                y_pred = np.asarray(model.predict(X_test))
            else:
                model = GradientBoostingRegressor(
                    n_estimators=max(50, int(cfg.ebm_max_rounds)),
                    learning_rate=max(1e-4, float(cfg.ebm_learning_rate)),
                    max_depth=3,
                    random_state=int(cfg.random_state),
                )
                model.fit(X_train, y_train)
                y_pred = np.asarray(model.predict(X_test))
                extra_note = "EBM no disponible en entorno; se uso aproximacion GradientBoostingRegressor."

        elif model_type == "BART":
            bart_ok = False
            bart_err = ""
            if BARTRegressor is not None:
                try:
                    model = BARTRegressor(n_trees=max(10, int(cfg.bart_num_trees)))
                    model.fit(X_train.values, y_train.values)
                    y_pred = np.asarray(model.predict(X_test.values))
                    bart_ok = True
                except Exception as e:
                    bart_err = str(e)

            if not bart_ok:
                model = GradientBoostingRegressor(
                    n_estimators=max(20, int(cfg.bart_num_trees)),
                    learning_rate=max(1e-4, float(cfg.bart_learning_rate)),
                    max_depth=max(1, int(cfg.bart_max_depth)),
                    subsample=0.8,
                    random_state=int(cfg.random_state),
                )
                model.fit(X_train, y_train)
                y_pred = np.asarray(model.predict(X_test))
                extra_note = "BART no disponible en entorno; se uso aproximacion aditiva con GradientBoostingRegressor."
                if bart_err:
                    extra_note += f" Error backend BART: {bart_err}"

        else:
            return RunResult(False, f"Tipo de modelo no soportado: {model_type}", model_type=model_type)

        p = 1 if model_type not in LINEAR_MODEL_TYPES else (X_test.shape[1] + (1 if cfg.include_intercept else 0))
        metrics = _ashrae_metrics(y_test.values, y_pred, p=p)
        metrics["train_size_rows"] = float(len(X_train))
        metrics["test_size_rows"] = float(len(X_test))

        suffix = _model_suffix(model_type)
        model_joblib_path = base + f"_{suffix}.joblib"
        joblib.dump(model, model_joblib_path)
        artifacts["model_joblib"] = model_joblib_path

        model_pkl_path = base + f"_{suffix}.pkl"
        with open(model_pkl_path, "wb") as f:
            pickle.dump(model, f)
        artifacts["model_pkl"] = model_pkl_path

        feature_scores = _extract_feature_scores(model, model_type, list(X.columns))
        if feature_scores:
            imp_json_path = base + f"_{suffix}_importances.json"
            with open(imp_json_path, "w", encoding="utf-8") as f:
                json.dump(feature_scores, f, ensure_ascii=False, indent=2)
            artifacts["importances_json"] = imp_json_path

            vals = np.asarray([feature_scores.get(c, 0.0) for c in X.columns], dtype=float)
            if model_type == "RF":
                artifacts["plot_rf_importances"] = _plot_rf_importances(list(X.columns), vals, base + "_importances.png")
            elif model_type in {"RIDGE", "LASSO"}:
                artifacts["plot_model_importances"] = _plot_feature_scores(
                    cols=list(X.columns),
                    vals=np.abs(vals),
                    out_path=base + "_importances.png",
                    title=f"{_model_label(model_type)}: |coeficientes| (top)",
                    xlabel="|coeficiente|",
                )
            else:
                artifacts["plot_model_importances"] = _plot_feature_scores(
                    cols=list(X.columns),
                    vals=vals,
                    out_path=base + "_importances.png",
                    title=f"{_model_label(model_type)}: Feature importances (top)",
                    xlabel="Importancia",
                )

        df_test = df_clean.loc[idx_test].copy()
        df_out = df_test[[cfg.date_col, cfg.target_col] + list(X.columns)].copy()
        df_out["y_pred"] = y_pred
        df_out["resid"] = df_out[cfg.target_col].astype(float) - df_out["y_pred"].astype(float)

        artifacts["plot_timeseries_html"] = _plot_timeseries_plotly_html(
            df_out, cfg.date_col, cfg.target_col, "y_pred", base + "_ts.html"
        )
        artifacts["plot_scatter"] = _plot_scatter(
            df_out[cfg.target_col].values, df_out["y_pred"].values, base + "_scatter.png"
        )
        artifacts["plot_residuals"] = _plot_residuals(df_out["resid"].values, base + "_resid.png")

        excel_path = base + "_export.xlsx"
        artifacts["excel_export"] = _export_excel(df_out, metrics, excel_path)

        meta = {
            "cfg": asdict(cfg),
            "timestamp": stamp,
            "date_col_used": cfg.date_col,
            "granularity_mode": granularity_mode,
            "granularity_detected": granularity_detected,
            "granularity_used": granularity_used,
            "features_used": list(X.columns),
            "features_dropped": all_nan_cols,
            "n_total_raw": n_total,
            "n_used": n_used,
            "n_train": int(len(X_train)),
            "n_test": int(len(X_test)),
            "metrics": metrics,
        }
        if extra_note:
            meta["note"] = extra_note
        meta_path = base + "_meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        artifacts["meta"] = meta_path

        return RunResult(
            ok=True,
            message=f"Entrenamiento {_model_label(model_type)} completado (export + graficas; principal interactiva; metricas en TEST).",
            model_type=model_type,
            used_features=list(X.columns),
            dropped_features=all_nan_cols,
            n_total=n_total,
            n_used=n_used,
            metrics=metrics,
            artifacts=artifacts,
            extra={
                "date_col_used": cfg.date_col,
                "include_intercept": cfg.include_intercept,
                "note": extra_note,
                "granularity_mode": granularity_mode,
                "granularity_detected": granularity_detected,
                "granularity_used": granularity_used,
            },
        )

    except Exception as e:
        return RunResult(False, f"Error en entrenamiento: {e}", model_type=cfg.model_type)

# =============================================================================
# Demo savings
# =============================================================================
def _predict_baseline(model, model_type: ModelType, X: pd.DataFrame, include_intercept: bool) -> np.ndarray:
    required_cols = _required_feature_names(model, model_type, list(X.columns))
    Xp = _align_X_columns(X, required_cols, "prediccion")

    if model_type == "OLS":
        X_ols = sm.add_constant(Xp, has_constant="add") if include_intercept else Xp
        return np.asarray(model.predict(X_ols.astype(float)))

    if model_type == "GAM":
        Xg = Xp.copy()
        smoother = getattr(getattr(model, "model", None), "smoother", None)
        sx = np.asarray(getattr(smoother, "x", []), dtype=float)
        if sx.ndim == 2 and sx.shape[1] == Xg.shape[1]:
            for i, col in enumerate(Xg.columns):
                lo = float(np.nanmin(sx[:, i]))
                hi = float(np.nanmax(sx[:, i]))
                Xg[col] = Xg[col].clip(lower=lo, upper=hi)
        exog = sm.add_constant(Xg, has_constant="add") if include_intercept else Xg
        return np.asarray(model.predict(exog=exog.astype(float), exog_smooth=Xg.astype(float)))

    if model_type == "BART":
        try:
            return np.asarray(model.predict(Xp))
        except Exception:
            return np.asarray(model.predict(Xp.values))

    return np.asarray(model.predict(Xp))


def demo_savings(
    model_path: str,
    model_type: ModelType,
    excel_path: str,
    sheet: str,
    date_col: str,
    target_col: str,
    feature_cols: List[str],
    date_start: str = "1900-01-01",
    date_end: str = "2100-01-01",
    include_ranges: Optional[List[Tuple[str, str]]] = None,
    exclude_dates: Optional[List[str]] = None,
    hour_rules: Optional[List[HourRule]] = None,
    granularity_mode: GranularityMode = "auto",
    include_intercept: bool = True,
    ols_r2_no_intercept_mode: R2NoInterceptMode = "centered",
    out_dir: str = "outputs",
    run_name: str = "palbe_demo",
) -> RunResult:
    try:
        model_type = _normalize_model_type(model_type)
        granularity_mode = _normalize_granularity_mode(granularity_mode)
        _ensure_excel_exists(excel_path)
        if not os.path.exists(model_path):
            return RunResult(False, f"Modelo no encontrado: {model_path}", model_type=model_type)

        os.makedirs(out_dir, exist_ok=True)

        df = pd.read_excel(excel_path, sheet_name=sheet)
        n_total = int(len(df))

        for c in [date_col, target_col]:
            if c not in df.columns:
                return RunResult(False, f"Columna '{c}' no existe en la hoja demo.", model_type=model_type)

        X_cols = [c for c in feature_cols if c not in (date_col, target_col)]
        if not X_cols:
            return RunResult(False, "Tras excluir fecha e y, no queda ninguna X en demo.", model_type=model_type)

        cfg_tmp = TrainConfig(
            excel_path=excel_path,
            sheet=sheet,
            date_col=date_col,
            target_col=target_col,
            feature_cols=X_cols,
            date_start=date_start,
            date_end=date_end,
            include_ranges=include_ranges or [],
            exclude_dates=exclude_dates or [],
            hour_rules=hour_rules or [],
            granularity_mode=granularity_mode,
            model_type=model_type,
            include_intercept=include_intercept,
            ols_r2_no_intercept_mode=_normalize_r2_no_intercept_mode(ols_r2_no_intercept_mode),
            out_dir=out_dir,
            model_name=run_name,
        )

        mask = _build_date_mask(df[date_col], cfg_tmp)
        mask = _apply_hour_rules(mask, df[date_col], cfg_tmp.hour_rules)
        df_f = df.loc[mask].copy()

        df_clean, y, X, all_nan_cols = _clean_xy(df_f, date_col, target_col, X_cols)
        n_used = int(len(df_clean))
        granularity_detected, granularity_used, granularity_mode = _resolve_granularity(
            df_clean[date_col] if n_used else pd.Series(dtype="datetime64[ns]"),
            granularity_mode,
        )
        if n_used < 2:
            return RunResult(
                False,
                "Muy pocas filas en demo tras filtros/limpieza.",
                model_type=model_type,
                extra={
                    "granularity_mode": granularity_mode,
                    "granularity_detected": granularity_detected,
                    "granularity_used": granularity_used,
                },
            )

        model = joblib.load(model_path)
        required_cols = _required_feature_names(model, model_type, list(X.columns))
        X_pred = _align_X_columns(X, required_cols, "demo")
        baseline = _predict_baseline(model, model_type, X_pred, include_intercept)

        savings = float(np.sum(baseline - y.values))
        p = 1 if model_type not in LINEAR_MODEL_TYPES else (X_pred.shape[1] + (1 if include_intercept else 0))
        ols_r2_mode = "centered"
        if model_type == "OLS" and not include_intercept:
            ols_r2_mode = _normalize_r2_no_intercept_mode(ols_r2_no_intercept_mode)
        metrics = _ashrae_metrics(y.values, baseline, p=p, r2_mode=cast(R2NoInterceptMode, ols_r2_mode))
        metrics["Ahorro_total_(baseline-real)"] = savings
        metrics["Baseline_total"] = float(np.sum(baseline))
        metrics["Real_total"] = float(np.sum(y.values))

        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(out_dir, f"{run_name}_{model_type}_{stamp}")

        artifacts: Dict[str, str] = {}
        artifacts["plot_weekly_completeness_html"] = _plot_weekly_completeness_heatmap_html(
            df_f,
            date_col,
            [target_col] + list(X_cols),
            base + "_weekly_completeness.html",
        )

        df_out = df_clean[[date_col, target_col] + list(X.columns)].copy()
        df_out["baseline_pred"] = baseline
        df_out["ahorro_inst_(baseline-real)"] = df_out["baseline_pred"] - df_out[target_col].astype(float)

        # Principal (interactivo): baseline_pred como y_pred
        tmp = df_out.rename(columns={"baseline_pred": "y_pred"}).copy()
        artifacts["plot_timeseries_html"] = _plot_timeseries_plotly_html(
            tmp, date_col, target_col, "y_pred", base + "_ts.html"
        )

        # Secundarias
        artifacts["plot_scatter"] = _plot_scatter(df_out[target_col].values, tmp["y_pred"].values, base + "_scatter.png")
        artifacts["plot_residuals"] = _plot_residuals((df_out[target_col].values - tmp["y_pred"].values), base + "_resid.png")

        excel_path_out = base + "_export.xlsx"
        artifacts["excel_export"] = _export_excel(df_out, metrics, excel_path_out)

        return RunResult(
            ok=True,
            message="Periodo demostrativo completado (baseline=predicción; ahorro=baseline-real) con export y gráfica principal interactiva.",
            model_type=model_type,
            used_features=list(X.columns),
            dropped_features=all_nan_cols,
            n_total=n_total,
            n_used=n_used,
            metrics=metrics,
            artifacts=artifacts,
            extra={
                "date_col_used": date_col,
                "include_intercept": include_intercept,
                "ols_r2_no_intercept_mode": ols_r2_mode,
                "granularity_mode": granularity_mode,
                "granularity_detected": granularity_detected,
                "granularity_used": granularity_used,
            },
        )

    except Exception as e:
        return RunResult(False, f"Error en periodo demostrativo: {e}", model_type=model_type)



##uvicorn app_palbe:app --reload

