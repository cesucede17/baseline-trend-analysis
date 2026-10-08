"""
palbe_db.py — SQLite database manager for PALBE v5
Projects → Sessions → Iterations hierarchy + multi-user auth + audit log
"""
import logging
import sqlite3
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any

_logger = logging.getLogger("palbe.db")


def _log_alter_table_error(table: str, column_def: str, exc: Exception) -> None:
    """Los ALTER TABLE de init_db() son idempotentes: 'duplicate column
    name' es el caso NORMAL en cada arranque (la columna ya existe) y no
    debe generar ruido. Cualquier otro error si se registra -- antes se
    tragaba en silencio con un except/pass."""
    if "duplicate column name" in str(exc).lower():
        return
    _logger.error("ALTER TABLE %s ADD COLUMN %s fallo: %s", table, column_def, exc)

DB_PATH = os.environ.get("PALBE_DB_PATH") or os.path.join(os.path.dirname(__file__), "palbe.db")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class User:
    id: int
    username: str
    email: str
    password_hash: str
    role: str          # 'admin' | 'user'
    color: str
    created_at: str
    is_active: int     # 1 = active, 0 = deactivated
    display_name: str = ""


@dataclass
class Project:
    id: int
    name: str
    description: str
    folder_path: str
    created_at: str
    owner_id: Optional[int] = None
    is_active: int = 1


@dataclass
class Session:
    id: int
    project_id: int
    name: str
    excel_path: str
    sheet: str
    date_col: str
    date_start: str
    date_end: str
    target_col: str
    feature_cols: List[str]
    created_at: str
    notes: str
    ipmvp_proyecto: str = ""
    ipmvp_cliente_sitio: str = ""
    ipmvp_contexto: str = ""
    ipmvp_periodo: str = ""
    ipmvp_frecuencia: str = "desconocida"
    ipmvp_unidades: str = "{}"
    ipmvp_criterio_outliers: str = "combinado"
    ipmvp_nivel_detalle: str = "tecnico"
    ipmvp_option: str = "C"
    ipmvp_note: str = ""
    user_id: Optional[int] = None
    ref_start: str = ""
    ref_end: str = ""
    demo_enabled: int = 0
    demo_start: str = ""
    demo_end: str = ""


@dataclass
class Iteration:
    id: int
    session_id: int
    name: str
    model_type: str
    model_name: str
    model_path: str
    r2: float
    rmse: float
    cvrmse: float
    excluded_rows: List[int]
    created_at: str
    notes: str
    include_intercept: bool = True
    ols_r2_no_intercept_mode: str = "centered"
    user_id: Optional[int] = None
    is_saved: int = 0          # 0 = draft, 1 = saved/public
    phase: str = "train"       # 'train' | 'demo'
    mae: Optional[float] = None
    nmbe: Optional[float] = None
    n_train: Optional[int] = None
    n_test: Optional[int] = None
    model_state: str = "trained"   # trained | saved | production | archived
    tags: List[str] = field(default_factory=list)
    input_signature: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActiveContext:
    project_id: Optional[int]
    project_name: Optional[str]
    session_id: Optional[int]
    session_name: Optional[str]
    excel_path: Optional[str]
    sheet: Optional[str]
    date_start: Optional[str]
    date_end: Optional[str]
    target_col: Optional[str]
    feature_cols: Optional[List[str]]
    iteration_id: Optional[int]
    iteration_name: Optional[str]
    model_type: Optional[str]
    model_name: Optional[str]
    model_path: Optional[str]
    r2: Optional[float]
    rmse: Optional[float]
    mode: str   # "train" | "demo"
    step: int   # 1-5
    # Cuando se movio esta persona por ultima vez en su contexto. La escribe
    # set_active_context, o sea la NAVEGACION normal -- la ruta de contexto del
    # recibidor sigue sin escribir nada. None en las filas anteriores al
    # 2026-09-18, que no tendran fecha hasta que esa persona vuelva a navegar.
    visto_en: Optional[str] = None


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Backup utilities — copia online de la DB usando la API sqlite3.backup()
# ---------------------------------------------------------------------------

BACKUP_DIR = os.environ.get("PALBE_BACKUP_DIR") or os.path.join(os.path.dirname(__file__), "palbe_backups")
BACKUP_KEEP = 10  # Mantener los N archivos más recientes


def _ensure_backup_dir() -> str:
    """Crea la carpeta de backups si no existe. Devuelve su ruta absoluta."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return BACKUP_DIR


def create_backup() -> dict:
    """
    Genera un backup atómico de palbe.db en BACKUP_DIR usando la API
    sqlite3.Connection.backup() — segura online, no requiere parar la app.

    Flujo:
      1. Escribe a archivo temporal `.partial` (atomicidad).
      2. Renombra a nombre final `palbe-YYYYMMDD-HHMMSS.db` sólo si la copia OK.
      3. Rota: elimina los más antiguos manteniendo los BACKUP_KEEP más recientes.

    Returns:
      dict con keys: ok (bool), path (str|None), filename (str|None),
                     size_bytes (int|None), timestamp (str|None), error (str|None)
    """
    try:
        backup_dir = _ensure_backup_dir()
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        final_name = f"palbe-{ts}.db"
        final_path = os.path.join(backup_dir, final_name)
        partial_path = final_path + ".partial"

        # Eliminar partial anterior si quedó huérfano
        if os.path.exists(partial_path):
            try: os.remove(partial_path)
            except OSError: pass

        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(partial_path)
            try:
                src.backup(dst)  # API atómica de sqlite3
            finally:
                dst.close()
        finally:
            src.close()

        # Rename atómico (Windows: os.replace funciona si destino no existe)
        os.replace(partial_path, final_path)

        size = os.path.getsize(final_path)

        # Rotación: lista los .db en BACKUP_DIR, mantiene los BACKUP_KEEP más recientes
        try:
            existing = sorted(
                (f for f in os.listdir(backup_dir)
                 if f.startswith("palbe-") and f.endswith(".db")),
                reverse=True,
            )
            for stale in existing[BACKUP_KEEP:]:
                try: os.remove(os.path.join(backup_dir, stale))
                except OSError: pass
        except OSError:
            pass

        return {
            "ok": True,
            "path": final_path,
            "filename": final_name,
            "size_bytes": size,
            "timestamp": ts,
        }
    except Exception as e:
        # Limpieza si hubo error a medias
        try:
            if os.path.exists(partial_path):
                os.remove(partial_path)
        except Exception:
            pass
        return {"ok": False, "error": str(e), "path": None, "filename": None,
                "size_bytes": None, "timestamp": None}


def list_backups() -> List[dict]:
    """Lista backups ordenados de más reciente a más antiguo."""
    try:
        backup_dir = _ensure_backup_dir()
        out = []
        for f in sorted(os.listdir(backup_dir), reverse=True):
            if not (f.startswith("palbe-") and f.endswith(".db")):
                continue
            full = os.path.join(backup_dir, f)
            try:
                st = os.stat(full)
                out.append({
                    "filename": f,
                    "path": full,
                    "size_bytes": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
                })
            except OSError:
                continue
        return out
    except OSError:
        return []


def get_latest_backup_info() -> Optional[dict]:
    """Devuelve info del backup más reciente o None si no hay."""
    items = list_backups()
    return items[0] if items else None


def _seed_users(conn: sqlite3.Connection) -> None:
    """Insert default users if the users table is empty."""
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count > 0:
        return
    import bcrypt as _bcrypt
    def _h(pw: str) -> str:
        return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()
    now = datetime.now().isoformat()
    users = [
        ("usuario1", "", _h("Palbe2026!"), "admin", "#16a34a", now, 1),
        ("usuario2", "", _h("User2026!"),  "user",  "#2563eb", now, 1),
    ]
    conn.executemany(
        "INSERT INTO users (username,email,password_hash,role,color,created_at,is_active) VALUES (?,?,?,?,?,?,?)",
        users
    )


def _backfill_from_meta(conn: sqlite3.Connection, outputs_dir: str) -> int:
    """Read meta.json files and populate new DB fields for existing iterations."""
    import glob as _glob, re as _re
    count = 0
    meta_files = _glob.glob(os.path.join(outputs_dir, "*_meta.json"))
    for mf in meta_files:
        try:
            with open(mf, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue

        stem = os.path.basename(mf).replace("_meta.json", "")
        row = conn.execute(
            "SELECT id FROM iterations WHERE model_path LIKE ? AND mae IS NULL",
            (f"%{stem}%",),
        ).fetchone()
        if not row:
            continue
        iid = row[0]

        metrics = meta.get("metrics", {})
        conn.execute(
            "UPDATE iterations SET mae=?, nmbe=?, n_train=?, n_test=? WHERE id=?",
            (metrics.get("MAE"), metrics.get("NMBE"),
             meta.get("n_used"), meta.get("n_total_raw", 0) - meta.get("n_used", 0) if meta.get("n_total_raw") else None,
             iid),
        )

        existing_params = conn.execute(
            "SELECT COUNT(*) FROM iteration_params WHERE iteration_id=?", (iid,)
        ).fetchone()[0]
        if not existing_params:
            cfg = meta.get("cfg", {})
            for k, v in cfg.items():
                if not isinstance(v, (str, int, float, bool, type(None))):
                    continue
                ptype = type(v).__name__ if v is not None else "None"
                conn.execute(
                    "INSERT OR IGNORE INTO iteration_params (iteration_id, param_key, param_value, param_type) "
                    "VALUES (?,?,?,?)",
                    (iid, k, str(v) if v is not None else "", ptype),
                )

        existing_feats = conn.execute(
            "SELECT COUNT(*) FROM iteration_features WHERE iteration_id=?", (iid,)
        ).fetchone()[0]
        if not existing_feats:
            for fname in meta.get("features_used", []):
                conn.execute(
                    "INSERT OR IGNORE INTO iteration_features (iteration_id, feature_name, importance, is_dropped) "
                    "VALUES (?,?,NULL,0)",
                    (iid, fname),
                )
            for fname in meta.get("features_dropped", []):
                conn.execute(
                    "INSERT OR IGNORE INTO iteration_features (iteration_id, feature_name, importance, is_dropped) "
                    "VALUES (?,?,NULL,1)",
                    (iid, fname),
                )

            imp_json_path = mf.replace("_meta.json", "_importances.json")
            if os.path.exists(imp_json_path):
                try:
                    with open(imp_json_path, "r", encoding="utf-8") as f:
                        imp_list = json.load(f)
                    for entry in imp_list:
                        conn.execute(
                            "UPDATE iteration_features SET importance=? "
                            "WHERE iteration_id=? AND feature_name=?",
                            (entry.get("importance"), iid, entry.get("feature")),
                        )
                except Exception:
                    pass
            else:
                ols_stats = meta.get("ols_variable_stats", [])
                for s in ols_stats:
                    if s.get("variable") and s["variable"] != "(intercepto)":
                        conn.execute(
                            "UPDATE iteration_features SET importance=? "
                            "WHERE iteration_id=? AND feature_name=?",
                            (abs(s["coef"]), iid, s["variable"]),
                        )

        else:
            null_imp = conn.execute(
                "SELECT COUNT(*) FROM iteration_features WHERE iteration_id=? AND importance IS NULL",
                (iid,),
            ).fetchone()[0]
            if null_imp:
                ols_stats = meta.get("ols_variable_stats", [])
                for s in ols_stats:
                    if s.get("variable") and s["variable"] != "(intercepto)":
                        conn.execute(
                            "UPDATE iteration_features SET importance=? "
                            "WHERE iteration_id=? AND feature_name=? AND importance IS NULL",
                            (abs(s["coef"]), iid, s["variable"]),
                        )

        count += 1
    return count


def init_db() -> None:
    """Create tables if they don't exist and run idempotent migrations."""
    conn = _get_conn()
    with conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT DEFAULT '',
            password_hash TEXT NOT NULL,
            role          TEXT DEFAULT 'user',
            color         TEXT DEFAULT '#16a34a',
            created_at    TEXT NOT NULL,
            is_active     INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS projects (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            description TEXT DEFAULT '',
            folder_path TEXT DEFAULT '',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_users (
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id    INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
            role       TEXT DEFAULT 'member',
            added_at   TEXT NOT NULL,
            PRIMARY KEY (project_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name         TEXT NOT NULL,
            excel_path   TEXT DEFAULT '',
            sheet        TEXT DEFAULT '',
            date_col     TEXT DEFAULT '',
            date_start   TEXT DEFAULT '',
            date_end     TEXT DEFAULT '',
            target_col   TEXT DEFAULT '',
            feature_cols TEXT DEFAULT '[]',
            created_at   TEXT NOT NULL,
            notes        TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS iterations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            name         TEXT NOT NULL,
            model_type   TEXT DEFAULT '',
            model_name   TEXT DEFAULT '',
            model_path   TEXT DEFAULT '',
            r2           REAL DEFAULT 0,
            rmse         REAL DEFAULT 0,
            cvrmse       REAL DEFAULT 0,
            excluded_rows TEXT DEFAULT '[]',
            created_at   TEXT NOT NULL,
            notes        TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS active_context (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            project_id    INTEGER,
            session_id    INTEGER,
            iteration_id  INTEGER,
            mode          TEXT DEFAULT 'train',
            step          INTEGER DEFAULT 1
        );

        -- Contexto activo POR USUARIO (trabajo simultáneo sin pisarse)
        CREATE TABLE IF NOT EXISTS active_context_user (
            user_id       INTEGER PRIMARY KEY,
            project_id    INTEGER,
            session_id    INTEGER,
            iteration_id  INTEGER,
            mode          TEXT DEFAULT 'train',
            step          INTEGER DEFAULT 1,
            -- Cuando estuvo aqui por ultima vez. Anadida el 2026-09-18 para la
            -- franja "Sigue donde lo dejaste" del recibidor: sin ella no habia
            -- NINGUNA fecha honesta que ensenar. Se midio en el servidor antes
            -- de decidirlo -- las alternativas eran la fecha de creacion del
            -- proyecto (meses atras) o las iteraciones guardadas, que en los
            -- datos reales eran cero.
            visto_en      TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     TEXT NOT NULL,
            user_id       INTEGER,
            action        TEXT NOT NULL,
            resource_type TEXT DEFAULT '',
            resource_id   INTEGER,
            details       TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS iteration_params (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            iteration_id INTEGER NOT NULL REFERENCES iterations(id) ON DELETE CASCADE,
            param_key    TEXT NOT NULL,
            param_value  TEXT DEFAULT '',
            param_type   TEXT DEFAULT 'str',
            UNIQUE(iteration_id, param_key)
        );

        CREATE TABLE IF NOT EXISTS iteration_features (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            iteration_id INTEGER NOT NULL REFERENCES iterations(id) ON DELETE CASCADE,
            feature_name TEXT NOT NULL,
            importance   REAL DEFAULT NULL,
            is_dropped   INTEGER DEFAULT 0,
            UNIQUE(iteration_id, feature_name)
        );

        -- Fase 4c: cruce entre identidades de Keycloak y usuarios de PALBE.
        -- Aditiva por diseno: la tabla users no se toca. El mapeo se puebla
        -- explicitamente (ver link_sso.py); NO se auto-provisiona, porque
        -- auto-provisionar es el camino directo a duplicar usuarios.
        CREATE TABLE IF NOT EXISTS sso_user_mapping (
            keycloak_sub      TEXT PRIMARY KEY,
            palbe_user_id     INTEGER NOT NULL REFERENCES users(id),
            keycloak_username TEXT,
            keycloak_email    TEXT,
            linked_at         TEXT NOT NULL,
            linked_by         INTEGER
        );

        INSERT OR IGNORE INTO active_context (id, project_id, session_id, iteration_id, mode, step)
        VALUES (1, NULL, NULL, NULL, 'train', 1);
        """)

        # Idempotent migrations — iterations
        for _col in [
            "include_intercept INTEGER DEFAULT 1",
            "ols_r2_no_intercept_mode TEXT DEFAULT 'centered'",
            "user_id INTEGER REFERENCES users(id)",
            "is_saved INTEGER DEFAULT 0",
            "phase TEXT DEFAULT 'train'",
            "mae REAL DEFAULT NULL",
            "nmbe REAL DEFAULT NULL",
            "n_train INTEGER DEFAULT NULL",
            "n_test INTEGER DEFAULT NULL",
            "model_state TEXT DEFAULT 'trained'",
            "tags TEXT DEFAULT '[]'",
            "input_signature TEXT DEFAULT '{}'",
        ]:
            try:
                conn.execute(f"ALTER TABLE iterations ADD COLUMN {_col}")
            except Exception as _e:
                _log_alter_table_error("iterations", _col, _e)

        # Idempotent migrations — sso_user_mapping
        # `keycloak_name` guarda el nombre de persona que manda Keycloak (el
        # claim `name`). Se anade el 2026-09-18 porque PALBE solo guardaba el
        # USUARIO, y por eso la misma persona salia como "cejemplo" en el bloque
        # "Trabajando ahora" y como "Carlos Ejemplo Pérez" en el recibidor.
        # Bartolo y Tambora ya lo tenian; PALBE era la unica sin el.
        for _col in ["keycloak_name TEXT DEFAULT ''"]:
            try:
                conn.execute(f"ALTER TABLE sso_user_mapping ADD COLUMN {_col}")
            except Exception as _e:
                _log_alter_table_error("sso_user_mapping", _col, _e)

        # Idempotent migrations — active_context_user
        # La tabla ya existe en el servidor sin esta columna, y CREATE TABLE IF
        # NOT EXISTS no la anade. Mismo patron que las de abajo.
        for _col in ["visto_en TEXT"]:
            try:
                conn.execute(f"ALTER TABLE active_context_user ADD COLUMN {_col}")
            except Exception as _e:
                _log_alter_table_error("active_context_user", _col, _e)

        # Idempotent migrations — sessions
        for _col in [
            "ipmvp_proyecto TEXT DEFAULT ''",
            "ipmvp_cliente_sitio TEXT DEFAULT ''",
            "ipmvp_contexto TEXT DEFAULT ''",
            "ipmvp_periodo TEXT DEFAULT ''",
            "ipmvp_frecuencia TEXT DEFAULT 'desconocida'",
            "ipmvp_unidades TEXT DEFAULT '{}'",
            "ipmvp_criterio_outliers TEXT DEFAULT 'combinado'",
            "ipmvp_nivel_detalle TEXT DEFAULT 'tecnico'",
            "ipmvp_option TEXT DEFAULT 'C'",
            "ipmvp_note TEXT DEFAULT ''",
            "user_id INTEGER REFERENCES users(id)",
            "ref_start TEXT DEFAULT ''",
            "ref_end TEXT DEFAULT ''",
            "demo_enabled INTEGER DEFAULT 0",
            "demo_start TEXT DEFAULT ''",
            "demo_end TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {_col}")
            except Exception as _e:
                _log_alter_table_error("sessions", _col, _e)

        # Idempotent migrations — users
        for _col in [
            "display_name TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {_col}")
            except Exception as _e:
                _log_alter_table_error("users", _col, _e)

        # Idempotent migrations — projects
        for _col in [
            "owner_id INTEGER REFERENCES users(id)",
            "is_active INTEGER DEFAULT 1",
        ]:
            try:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {_col}")
            except Exception as _e:
                _log_alter_table_error("projects", _col, _e)

        if os.environ.get("PALBE_SEED_DEMO_USERS") == "1":
            _seed_users(conn)

        # One-time backfill from meta.json (idempotent)
        _outputs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
        if os.path.isdir(_outputs):
            _backfill_from_meta(conn, _outputs)

    conn.close()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _row_to_user(d: dict) -> User:
    return User(
        id=d["id"], username=d["username"], email=d.get("email", ""),
        password_hash=d["password_hash"], role=d.get("role", "user"),
        color=d.get("color", "#16a34a"), created_at=d["created_at"],
        is_active=d.get("is_active", 1),
        display_name=d.get("display_name", ""),
    )


def create_user(username: str, password_hash: str, role: str = "user", color: str = "#16a34a", email: str = "") -> User:
    conn = _get_conn()
    now = datetime.now().isoformat()
    with conn:
        cur = conn.execute(
            "INSERT INTO users (username,email,password_hash,role,color,created_at,is_active) VALUES (?,?,?,?,?,?,1)",
            (username, email, password_hash, role, color, now)
        )
        uid = cur.lastrowid
    conn.close()
    return User(id=uid, username=username, email=email, password_hash=password_hash,
                role=role, color=color, created_at=now, is_active=1)


def get_user_by_id(user_id: int) -> Optional[User]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return _row_to_user(dict(row)) if row else None


def get_user_by_username(username: str) -> Optional[User]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return _row_to_user(dict(row)) if row else None


def buscar_usuarios_por_nombre_sin_caja(username: str) -> List[User]:
    """Los usuarios cuyo nombre coincide IGNORANDO mayusculas y minusculas.

    Devuelve una lista y no un usuario a proposito: si coincide mas de uno,
    quien llama tiene que decidir, y elegir por el seria repartir los
    proyectos de alguien a cara o cruz.

    La insensibilidad se pide en la consulta y no en la columna porque el
    esquema de `users` esta congelado (anadirle COLLATE NOCASE obliga a
    recrear la tabla). OJO: el COLLATE NOCASE de SQLite solo pliega ASCII,
    asi que 'Ñ' y 'ñ' NO se consideran iguales. Suficiente aqui: los nombres
    de usuario que llegan de Keycloak son ASCII.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM users WHERE username=? COLLATE NOCASE ORDER BY id",
        (username,),
    ).fetchall()
    conn.close()
    return [_row_to_user(dict(r)) for r in rows]


def list_users() -> List[User]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    conn.close()
    return [_row_to_user(dict(r)) for r in rows]


def update_user_role(user_id: int, role: str) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
    conn.close()


def update_user_color(user_id: int, color: str) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE users SET color=? WHERE id=?", (color, user_id))
    conn.close()


def deactivate_user(user_id: int) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE users SET is_active=0 WHERE id=?", (user_id,))
    conn.close()


def activate_user(user_id: int) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE users SET is_active=1 WHERE id=?", (user_id,))
    conn.close()


def update_user_password(user_id: int, password_hash: str) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
    conn.close()


def update_user_details(user_id: int, *, username: str = None,
                        display_name: str = None, email: str = None) -> None:
    sets, vals = [], []
    if username is not None:
        sets.append("username=?"); vals.append(username)
    if display_name is not None:
        sets.append("display_name=?"); vals.append(display_name)
    if email is not None:
        sets.append("email=?"); vals.append(email)
    if not sets:
        return
    vals.append(user_id)
    conn = _get_conn()
    with conn:
        conn.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", vals)
    conn.close()


# ---------------------------------------------------------------------------
# Project-User assignments
# ---------------------------------------------------------------------------

def assign_user_to_project(project_id: int, user_id: int, role: str = "member") -> None:
    conn = _get_conn()
    now = datetime.now().isoformat()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO project_users (project_id,user_id,role,added_at) VALUES (?,?,?,?)",
            (project_id, user_id, role, now)
        )
    conn.close()


def remove_user_from_project(project_id: int, user_id: int) -> None:
    conn = _get_conn()
    with conn:
        conn.execute(
            "DELETE FROM project_users WHERE project_id=? AND user_id=?",
            (project_id, user_id)
        )
    conn.close()


def get_project_users(project_id: int) -> List[User]:
    conn = _get_conn()
    rows = conn.execute(
        """SELECT u.* FROM users u
           JOIN project_users pu ON pu.user_id = u.id
           WHERE pu.project_id=?
           ORDER BY pu.added_at""",
        (project_id,)
    ).fetchall()
    conn.close()
    return [_row_to_user(dict(r)) for r in rows]


def get_user_projects(user_id: int) -> List[Project]:
    """Returns projects assigned to a user via project_users table."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT p.* FROM projects p
           JOIN project_users pu ON pu.project_id = p.id
           WHERE pu.user_id=?
           ORDER BY p.created_at DESC""",
        (user_id,)
    ).fetchall()
    conn.close()
    return [_row_to_project(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def _row_to_project(d: dict) -> Project:
    return Project(
        id=d["id"], name=d["name"], description=d.get("description", ""),
        folder_path=d.get("folder_path", ""), created_at=d["created_at"],
        owner_id=d.get("owner_id"),
        is_active=d.get("is_active", 1),
    )


def create_project(name: str, description: str = "", folder_path: str = "", owner_id: Optional[int] = None) -> Project:
    conn = _get_conn()
    now = datetime.now().isoformat()
    with conn:
        cur = conn.execute(
            "INSERT INTO projects (name, description, folder_path, created_at, owner_id) VALUES (?,?,?,?,?)",
            (name, description, folder_path, now, owner_id)
        )
        pid = cur.lastrowid
    conn.close()
    p = Project(id=pid, name=name, description=description, folder_path=folder_path,
                created_at=now, owner_id=owner_id)
    if owner_id:
        assign_user_to_project(pid, owner_id, role="owner")
    return p


def list_projects(include_inactive: bool = False) -> List[Project]:
    conn = _get_conn()
    if include_inactive:
        rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM projects WHERE is_active=1 ORDER BY created_at DESC").fetchall()
    conn.close()
    return [_row_to_project(dict(r)) for r in rows]


def get_project(project_id: int) -> Optional[Project]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    conn.close()
    return _row_to_project(dict(row)) if row else None


def delete_project(project_id: int) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    conn.close()


def deactivate_project(project_id: int) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE projects SET is_active=0 WHERE id=?", (project_id,))
    conn.close()


def activate_project(project_id: int) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE projects SET is_active=1 WHERE id=?", (project_id,))
    conn.close()


def update_project(project_id: int, name: Optional[str] = None, description: Optional[str] = None,
                   folder_path: Optional[str] = None) -> None:
    conn = _get_conn()
    fields, vals = [], []
    if name is not None:
        fields.append("name=?"); vals.append(name)
    if description is not None:
        fields.append("description=?"); vals.append(description)
    if folder_path is not None:
        fields.append("folder_path=?"); vals.append(folder_path)
    if fields:
        vals.append(project_id)
        with conn:
            conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id=?", vals)
    conn.close()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _row_to_session(d: dict) -> Session:
    d["feature_cols"] = json.loads(d.get("feature_cols", "[]"))
    if "date_col" not in d:
        d["date_col"] = ""
    for _f in ["ipmvp_proyecto", "ipmvp_cliente_sitio", "ipmvp_contexto",
               "ipmvp_periodo", "ipmvp_frecuencia", "ipmvp_unidades",
               "ipmvp_criterio_outliers", "ipmvp_nivel_detalle"]:
        if _f not in d:
            d[_f] = "" if _f != "ipmvp_frecuencia" else "desconocida"
    d.setdefault("ipmvp_option", "C")
    d.setdefault("ipmvp_note", "")
    d.setdefault("user_id", None)
    for _f in ["ref_start", "ref_end", "demo_start", "demo_end"]:
        d.setdefault(_f, "")
    d.setdefault("demo_enabled", 0)
    return Session(**d)


def create_session(
    project_id: int,
    name: str,
    excel_path: str = "",
    sheet: str = "",
    date_col: str = "",
    date_start: str = "",
    date_end: str = "",
    target_col: str = "",
    feature_cols: Optional[List[str]] = None,
    notes: str = "",
    user_id: Optional[int] = None,
) -> Session:
    conn = _get_conn()
    now = datetime.now().isoformat()
    fc_json = json.dumps(feature_cols or [])
    with conn:
        cur = conn.execute(
            """INSERT INTO sessions
               (project_id,name,excel_path,sheet,date_col,date_start,date_end,
                target_col,feature_cols,created_at,notes,user_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, name, excel_path, sheet, date_col, date_start, date_end,
             target_col, fc_json, now, notes, user_id)
        )
        sid = cur.lastrowid
    conn.close()
    return Session(
        id=sid, project_id=project_id, name=name, excel_path=excel_path,
        sheet=sheet, date_col=date_col, date_start=date_start, date_end=date_end,
        target_col=target_col, feature_cols=feature_cols or [],
        created_at=now, notes=notes, user_id=user_id,
    )


def list_sessions(project_id: int) -> List[Session]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE project_id=? ORDER BY created_at DESC", (project_id,)
    ).fetchall()
    conn.close()
    return [_row_to_session(dict(r)) for r in rows]


def get_session(session_id: int) -> Optional[Session]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_session(dict(row))


def update_session(
    session_id: int,
    excel_path: Optional[str] = None,
    sheet: Optional[str] = None,
    date_col: Optional[str] = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    target_col: Optional[str] = None,
    feature_cols: Optional[List[str]] = None,
    notes: Optional[str] = None,
    ref_start: Optional[str] = None,
    ref_end: Optional[str] = None,
    demo_enabled: Optional[int] = None,
    demo_start: Optional[str] = None,
    demo_end: Optional[str] = None,
    ipmvp_option: Optional[str] = None,
    ipmvp_note: Optional[str] = None,
) -> None:
    conn = _get_conn()
    fields, vals = [], []
    if excel_path is not None:
        fields.append("excel_path=?"); vals.append(excel_path)
    if sheet is not None:
        fields.append("sheet=?"); vals.append(sheet)
    if date_col is not None:
        fields.append("date_col=?"); vals.append(date_col)
    if date_start is not None:
        fields.append("date_start=?"); vals.append(date_start)
    if date_end is not None:
        fields.append("date_end=?"); vals.append(date_end)
    if target_col is not None:
        fields.append("target_col=?"); vals.append(target_col)
    if feature_cols is not None:
        fields.append("feature_cols=?"); vals.append(json.dumps(feature_cols))
    if notes is not None:
        fields.append("notes=?"); vals.append(notes)
    if ref_start is not None:
        fields.append("ref_start=?"); vals.append(ref_start)
    if ref_end is not None:
        fields.append("ref_end=?"); vals.append(ref_end)
    if demo_enabled is not None:
        fields.append("demo_enabled=?"); vals.append(demo_enabled)
    if demo_start is not None:
        fields.append("demo_start=?"); vals.append(demo_start)
    if demo_end is not None:
        fields.append("demo_end=?"); vals.append(demo_end)
    if ipmvp_option is not None:
        fields.append("ipmvp_option=?"); vals.append(ipmvp_option)
    if ipmvp_note is not None:
        fields.append("ipmvp_note=?"); vals.append(ipmvp_note)
    if fields:
        vals.append(session_id)
        with conn:
            conn.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE id=?", vals)
    conn.close()


def update_session_ipmvp(
    session_id: int,
    proyecto: str = "",
    cliente_sitio: str = "",
    contexto: str = "",
    periodo: str = "",
    frecuencia: str = "desconocida",
    unidades: str = "{}",
    criterio_outliers: str = "combinado",
    nivel_detalle: str = "tecnico",
    option: str = "C",
    note: str = "",
) -> None:
    conn = _get_conn()
    with conn:
        conn.execute(
            """UPDATE sessions SET
               ipmvp_proyecto=?, ipmvp_cliente_sitio=?, ipmvp_contexto=?,
               ipmvp_periodo=?, ipmvp_frecuencia=?, ipmvp_unidades=?,
               ipmvp_criterio_outliers=?, ipmvp_nivel_detalle=?,
               ipmvp_option=?, ipmvp_note=?
               WHERE id=?""",
            (proyecto, cliente_sitio, contexto, periodo, frecuencia,
             unidades, criterio_outliers, nivel_detalle, option, note, session_id)
        )
    conn.close()


def delete_session(session_id: int) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.close()


# ---------------------------------------------------------------------------
# Iterations
# ---------------------------------------------------------------------------

def _row_to_iteration(d: dict) -> Iteration:
    d["excluded_rows"] = json.loads(d.get("excluded_rows", "[]"))
    d["include_intercept"] = bool(d.get("include_intercept", 1))
    d["ols_r2_no_intercept_mode"] = d.get("ols_r2_no_intercept_mode", "centered") or "centered"
    d.setdefault("user_id", None)
    d.setdefault("is_saved", 0)
    d.setdefault("phase", "train")
    d.setdefault("mae", None)
    d.setdefault("nmbe", None)
    d.setdefault("n_train", None)
    d.setdefault("n_test", None)
    d.setdefault("model_state", "trained")
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["input_signature"] = json.loads(d.get("input_signature") or "{}")
    return Iteration(**d)


def add_iteration(
    session_id: int,
    name: str,
    model_type: str = "",
    model_name: str = "",
    model_path: str = "",
    r2: float = 0.0,
    rmse: float = 0.0,
    cvrmse: float = 0.0,
    excluded_rows: Optional[List[int]] = None,
    notes: str = "",
    include_intercept: bool = True,
    ols_r2_no_intercept_mode: str = "centered",
    user_id: Optional[int] = None,
    phase: str = "train",
    mae: Optional[float] = None,
    nmbe: Optional[float] = None,
    n_train: Optional[int] = None,
    n_test: Optional[int] = None,
) -> Iteration:
    conn = _get_conn()
    now = datetime.now().isoformat()
    er_json = json.dumps(excluded_rows or [])
    with conn:
        cur = conn.execute(
            """INSERT INTO iterations
               (session_id,name,model_type,model_name,model_path,r2,rmse,cvrmse,
                excluded_rows,created_at,notes,include_intercept,ols_r2_no_intercept_mode,
                user_id,is_saved,phase,mae,nmbe,n_train,n_test)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
            (session_id, name, model_type, model_name, model_path, r2, rmse, cvrmse,
             er_json, now, notes, int(include_intercept), ols_r2_no_intercept_mode,
             user_id, phase, mae, nmbe, n_train, n_test)
        )
        iid = cur.lastrowid
    conn.close()
    return Iteration(
        id=iid, session_id=session_id, name=name, model_type=model_type,
        model_name=model_name, model_path=model_path, r2=r2, rmse=rmse,
        cvrmse=cvrmse, excluded_rows=excluded_rows or [], created_at=now, notes=notes,
        include_intercept=include_intercept, ols_r2_no_intercept_mode=ols_r2_no_intercept_mode,
        user_id=user_id, is_saved=0, phase=phase,
        mae=mae, nmbe=nmbe, n_train=n_train, n_test=n_test,
    )


def list_iterations(
    session_id: int,
    requesting_user_id: Optional[int] = None,
) -> List[Iteration]:
    """
    requesting_user_id=None  → admin: returns all iterations
    requesting_user_id=X     → user: returns saved iterations + own drafts
    """
    conn = _get_conn()
    if requesting_user_id is None:
        rows = conn.execute(
            "SELECT * FROM iterations WHERE session_id=? ORDER BY created_at DESC",
            (session_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM iterations
               WHERE session_id=?
                 AND (is_saved=1 OR user_id=?)
               ORDER BY created_at DESC""",
            (session_id, requesting_user_id)
        ).fetchall()
    conn.close()
    return [_row_to_iteration(dict(r)) for r in rows]


def get_iteration(iteration_id: int) -> Optional[Iteration]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM iterations WHERE id=?", (iteration_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_iteration(dict(row))


def save_iteration(iteration_id: int) -> None:
    """Mark iteration as saved (public to all project members)."""
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE iterations SET is_saved=1 WHERE id=?", (iteration_id,))
    conn.close()


def discard_iteration(iteration_id: int) -> None:
    """Delete a draft iteration (and its model files) from DB."""
    it = get_iteration(iteration_id)
    if it and it.model_path and os.path.exists(it.model_path):
        try:
            os.remove(it.model_path)
        except OSError:
            pass
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM iterations WHERE id=?", (iteration_id,))
    conn.close()


def update_iteration_excluded_rows(iteration_id: int, excluded_rows: List[int]) -> None:
    conn = _get_conn()
    with conn:
        conn.execute(
            "UPDATE iterations SET excluded_rows=? WHERE id=?",
            (json.dumps(excluded_rows), iteration_id)
        )
    conn.close()


def delete_iteration(iteration_id: int) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM iterations WHERE id=?", (iteration_id,))
    conn.close()


def rename_iteration(iteration_id: int, new_name: str) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE iterations SET name=? WHERE id=?", (new_name.strip(), iteration_id))
    conn.close()


def list_all_iterations() -> List[dict]:
    """Admin view: all iterations across all sessions with project/session info."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT i.*, s.name AS session_name, p.name AS project_name,
                  u.username AS username
           FROM iterations i
           JOIN sessions s ON s.id = i.session_id
           JOIN projects p ON p.id = s.project_id
           LEFT JOIN users u ON u.id = i.user_id
           ORDER BY i.created_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Iteration Tracking — params, features, state, tags, signature
# ---------------------------------------------------------------------------

_PRIMITIVE_TYPES = (str, int, float, bool, type(None))


def save_iteration_params(iteration_id: int, params: Dict[str, Any]) -> None:
    conn = _get_conn()
    with conn:
        for k, v in params.items():
            if not isinstance(v, _PRIMITIVE_TYPES):
                continue
            ptype = type(v).__name__ if v is not None else "None"
            conn.execute(
                "INSERT OR REPLACE INTO iteration_params (iteration_id, param_key, param_value, param_type) "
                "VALUES (?,?,?,?)",
                (iteration_id, k, str(v) if v is not None else "", ptype),
            )
    conn.close()


def get_iteration_params(iteration_id: int) -> Dict[str, Any]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT param_key, param_value, param_type FROM iteration_params WHERE iteration_id=?",
        (iteration_id,),
    ).fetchall()
    conn.close()
    result: Dict[str, Any] = {}
    for r in rows:
        k, v, t = r["param_key"], r["param_value"], r["param_type"]
        if t == "int":
            try: result[k] = int(v)
            except ValueError: result[k] = v
        elif t == "float":
            try: result[k] = float(v)
            except ValueError: result[k] = v
        elif t == "bool":
            result[k] = v.lower() in ("true", "1")
        elif t == "None":
            result[k] = None
        else:
            result[k] = v
    return result


def save_iteration_features(
    iteration_id: int,
    used: List[str],
    dropped: List[str],
    importances: Optional[Dict[str, float]] = None,
) -> None:
    conn = _get_conn()
    with conn:
        for fname in used:
            imp = importances.get(fname) if importances else None
            conn.execute(
                "INSERT OR REPLACE INTO iteration_features (iteration_id, feature_name, importance, is_dropped) "
                "VALUES (?,?,?,0)",
                (iteration_id, fname, imp),
            )
        for fname in dropped:
            conn.execute(
                "INSERT OR REPLACE INTO iteration_features (iteration_id, feature_name, importance, is_dropped) "
                "VALUES (?,?,NULL,1)",
                (iteration_id, fname),
            )
    conn.close()


def get_iteration_features(iteration_id: int) -> List[Dict[str, Any]]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT feature_name, importance, is_dropped FROM iteration_features "
        "WHERE iteration_id=? ORDER BY importance DESC NULLS LAST",
        (iteration_id,),
    ).fetchall()
    conn.close()
    return [{"name": r["feature_name"], "importance": r["importance"], "is_dropped": bool(r["is_dropped"])} for r in rows]


def get_iteration_feature_names(iteration_id: int) -> set:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT feature_name FROM iteration_features WHERE iteration_id=? AND is_dropped=0",
        (iteration_id,),
    ).fetchall()
    conn.close()
    return {r["feature_name"] for r in rows}


def update_model_state(iteration_id: int, new_state: str) -> None:
    valid = {"trained", "saved", "production", "archived"}
    if new_state not in valid:
        return
    conn = _get_conn()
    with conn:
        if new_state == "production":
            row = conn.execute("SELECT session_id FROM iterations WHERE id=?", (iteration_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE iterations SET model_state='saved' WHERE session_id=? AND model_state='production'",
                    (row["session_id"],),
                )
        conn.execute("UPDATE iterations SET model_state=? WHERE id=?", (new_state, iteration_id))
        if new_state in ("saved", "production"):
            conn.execute("UPDATE iterations SET is_saved=1 WHERE id=?", (iteration_id,))
    conn.close()


def update_iteration_tags(iteration_id: int, tags: List[str]) -> None:
    seen: set = set()
    clean: List[str] = []
    for t in tags:
        t = t.strip()[:50]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            clean.append(t)
        if len(clean) >= 10:
            break
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE iterations SET tags=? WHERE id=?", (json.dumps(clean), iteration_id))
    conn.close()


def save_iteration_signature(iteration_id: int, sig: Dict[str, Any]) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE iterations SET input_signature=? WHERE id=?", (json.dumps(sig), iteration_id))
    conn.close()


def get_production_iteration(session_id: int) -> Optional[Iteration]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM iterations WHERE session_id=? AND model_state='production'",
        (session_id,),
    ).fetchone()
    conn.close()
    return _row_to_iteration(dict(row)) if row else None


def get_iterations_by_ids(ids: List[int]) -> List[Iteration]:
    if not ids:
        return []
    conn = _get_conn()
    ph = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT * FROM iterations WHERE id IN ({ph})", ids).fetchall()
    conn.close()
    return [_row_to_iteration(dict(r)) for r in rows]


def get_comparable_iterations(project_id: int) -> Dict[str, List[int]]:
    """Group project iterations by their feature set. Returns {feature_key: [iteration_ids]}."""
    conn = _get_conn()
    session_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM sessions WHERE project_id=?", (project_id,)
    ).fetchall()]
    if not session_ids:
        conn.close()
        return {}
    ph = ",".join("?" for _ in session_ids)
    iters = conn.execute(
        f"SELECT id FROM iterations WHERE session_id IN ({ph}) AND model_path IS NOT NULL AND model_path != ''",
        session_ids,
    ).fetchall()
    groups: Dict[str, List[int]] = {}
    for row in iters:
        it_id = row["id"]
        feat_rows = conn.execute(
            "SELECT feature_name FROM iteration_features WHERE iteration_id=? AND is_dropped=0 ORDER BY feature_name",
            (it_id,),
        ).fetchall()
        key = ",".join(r["feature_name"] for r in feat_rows) if feat_rows else ""
        groups.setdefault(key, []).append(it_id)
    conn.close()
    return groups


# ---------------------------------------------------------------------------
# Audit Log
# ---------------------------------------------------------------------------

def log_action(
    user_id: Optional[int],
    action: str,
    resource_type: str = "",
    resource_id: Optional[int] = None,
    details: Optional[dict] = None,
) -> None:
    conn = _get_conn()
    now = datetime.now().isoformat()
    with conn:
        conn.execute(
            """INSERT INTO audit_log (timestamp,user_id,action,resource_type,resource_id,details)
               VALUES (?,?,?,?,?,?)""",
            (now, user_id, action, resource_type, resource_id, json.dumps(details or {}))
        )
    conn.close()


def list_audit_log(limit: int = 200) -> List[dict]:
    conn = _get_conn()
    rows = conn.execute(
        """SELECT al.*, u.username
           FROM audit_log al
           LEFT JOIN users u ON u.id = al.user_id
           ORDER BY al.timestamp DESC
           LIMIT ?""",
        (limit,)
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["details"] = json.loads(d.get("details", "{}"))
        except Exception:
            d["details"] = {}
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Active Context — POR USUARIO
# Cada usuario tiene su propio contexto activo (proyecto/sesión/iteración/paso),
# para permitir que varios usuarios trabajen a la vez sin pisarse.
# El user_id se obtiene de una variable de contexto fijada por petición desde
# el middleware de la app (set_current_user_id). Si no hay usuario → clave 0.
# ---------------------------------------------------------------------------
import contextvars as _contextvars
_CURRENT_USER_ID = _contextvars.ContextVar("palbe_current_user_id", default=None)


def set_current_user_id(uid) -> None:
    """Fija el usuario de la petición actual (lo invoca el middleware de la app)."""
    try:
        _CURRENT_USER_ID.set(int(uid) if uid is not None else None)
    except (TypeError, ValueError):
        _CURRENT_USER_ID.set(None)


def _active_ctx_key() -> int:
    uid = _CURRENT_USER_ID.get()
    return int(uid) if uid is not None else 0


def set_active_context(
    project_id: Optional[int] = None,
    session_id: Optional[int] = None,
    iteration_id: Optional[int] = None,
    mode: Optional[str] = None,
    step: Optional[int] = None,
) -> None:
    uid = _active_ctx_key()
    conn = _get_conn()
    fields, vals = [], []
    if project_id is not None:
        fields.append("project_id=?"); vals.append(project_id)
    if session_id is not None:
        fields.append("session_id=?"); vals.append(session_id)
    if iteration_id is not None:
        fields.append("iteration_id=?"); vals.append(iteration_id if iteration_id else None)
    if mode is not None:
        fields.append("mode=?"); vals.append(mode)
    if step is not None:
        fields.append("step=?"); vals.append(step)
    # La marca de tiempo se pone SIEMPRE, incluso si no cambia ningun otro
    # campo: pasar por aqui es haber estado, y eso es justo lo que la franja
    # del recibidor necesita saber.
    fields.append("visto_en=?"); vals.append(datetime.now().isoformat())
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO active_context_user (user_id, mode, step) VALUES (?, 'train', 1)",
            (uid,),
        )
        if fields:
            vals.append(uid)
            conn.execute(f"UPDATE active_context_user SET {', '.join(fields)} WHERE user_id=?", vals)
    conn.close()


def get_active_context() -> ActiveContext:
    uid = _active_ctx_key()
    conn = _get_conn()
    row = conn.execute("SELECT * FROM active_context_user WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    if not row:
        return ActiveContext(
            project_id=None, project_name=None, session_id=None, session_name=None,
            excel_path=None, sheet=None, date_start=None, date_end=None,
            target_col=None, feature_cols=None, iteration_id=None,
            iteration_name=None, model_type=None, model_name=None,
            model_path=None, r2=None, rmse=None, mode="train", step=1,
            visto_en=None,
        )
    d = dict(row)
    project_name = session_name = excel_path = sheet = None
    date_start = date_end = target_col = feature_cols = None
    iteration_name = model_type = model_name = model_path = r2 = rmse = None

    if d["project_id"]:
        p = get_project(d["project_id"])
        if p:
            project_name = p.name

    if d["session_id"]:
        s = get_session(d["session_id"])
        if s:
            session_name = s.name
            excel_path = s.excel_path
            sheet = s.sheet
            date_start = s.date_start
            date_end = s.date_end
            target_col = s.target_col
            feature_cols = s.feature_cols

    if d["iteration_id"]:
        it = get_iteration(d["iteration_id"])
        if it:
            iteration_name = it.name
            model_type = it.model_type
            model_name = it.model_name
            model_path = it.model_path
            r2 = it.r2
            rmse = it.rmse

    return ActiveContext(
        project_id=d["project_id"], project_name=project_name,
        session_id=d["session_id"], session_name=session_name,
        excel_path=excel_path, sheet=sheet, date_start=date_start, date_end=date_end,
        target_col=target_col, feature_cols=feature_cols,
        iteration_id=d["iteration_id"], iteration_name=iteration_name,
        model_type=model_type, model_name=model_name, model_path=model_path,
        r2=r2, rmse=rmse, mode=d["mode"], step=d["step"],
        # .get y no [] a proposito: una base a la que todavia no se le ha
        # aplicado la migracion (init_db corre al arrancar, no al copiar un
        # fichero) daria KeyError y un 500 en CUALQUIER pagina de PALBE, no
        # solo en la franja. Se descubrio asi: 30 tests en rojo.
        visto_en=d.get("visto_en"),
    )


def clear_active_context() -> None:
    uid = _active_ctx_key()
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM active_context_user WHERE user_id=?", (uid,))
    conn.close()


# ---------------------------------------------------------------------------
# Fase 4c: cruce de identidades de Keycloak con usuarios de PALBE
# ---------------------------------------------------------------------------

def get_sso_mapping(keycloak_sub: str) -> Optional[int]:
    """Devuelve el id de usuario de PALBE mapeado a ese sub, o None.

    None significa "no vinculado", y el llamante debe negar el acceso --
    nunca crear el usuario."""
    conn = _get_conn()
    fila = conn.execute(
        "SELECT palbe_user_id FROM sso_user_mapping WHERE keycloak_sub=?",
        (keycloak_sub,),
    ).fetchone()
    conn.close()
    return int(fila[0]) if fila else None


def get_sso_sub_de_usuario(palbe_user_id: int) -> Optional[str]:
    """El sub de Keycloak vinculado a ese usuario de PALBE, o None.

    La pregunta inversa de get_sso_mapping, y existe por un caso concreto:
    antes de cruzar por nombre de usuario hay que saber si ese usuario ya
    pertenece a OTRA identidad de Keycloak. Si pertenece, no es un alta, es
    una colision de nombres y hay que negarla.

    Aditiva y de SOLO LECTURA, como get_sso_mapping: el monolito esta
    congelado y una comodidad de consulta no toca el esquema."""
    conn = _get_conn()
    fila = conn.execute(
        "SELECT keycloak_sub FROM sso_user_mapping WHERE palbe_user_id=?",
        (palbe_user_id,),
    ).fetchone()
    conn.close()
    return str(fila[0]) if fila else None


def list_companeros_de_proyecto(
    project_id: int, excepto_user_id: int, limite: int = 3
) -> List[Dict[str, Any]]:
    """Quien mas ha trabajado en ese proyecto, y cuando fue la ultima vez.

    Para la franja "Sigue donde lo dejaste" del recibidor (ver
    palbe_contexto.py). Aditiva y de SOLO LECTURA, como get_sso_mapping en la
    Fase 4c: el monolito esta congelado y una comodidad de consulta no es una
    correccion, asi que no se toca ni el esquema ni nada que escriba.

    La fecha es DERIVADA, y esa es la parte que hay que entender: la tabla
    active_context_user sabe DONDE esta cada uno pero no CUANDO estuvo -- no
    tiene columna de tiempo. Y audit_log, que si tiene timestamp, no tiene
    project_id. Asi que la ultima actividad de una persona en un proyecto se
    saca del created_at de sus iteraciones, subiendo iterations -> sessions
    -> projects. Quien no ha creado ninguna no sale: el dato existe para
    avisar de que alguien puede pisarte, y quien no ha tocado nada no te va a
    pisar.

    El alcance de privacidad no se comprueba aqui a proposito: quien llama ya
    ha verificado que puede ver ese proyecto (palbe_contexto._puede_ver)."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT u.username AS nombre, MAX(i.created_at) AS visto_en
           FROM iterations i
           JOIN sessions s ON s.id = i.session_id
           JOIN users u    ON u.id = i.user_id
           WHERE s.project_id = ? AND i.user_id IS NOT NULL AND i.user_id != ?
           GROUP BY i.user_id
           ORDER BY visto_en DESC
           LIMIT ?""",
        (project_id, excepto_user_id, limite),
    ).fetchall()
    conn.close()
    return [{"nombre": r["nombre"], "visto_en": r["visto_en"]} for r in rows]


def create_sso_mapping(
    keycloak_sub: str,
    palbe_user_id: int,
    keycloak_username: str = "",
    keycloak_email: str = "",
    linked_by: Optional[int] = None,
) -> None:
    """Vincula una identidad de Keycloak con un usuario existente de PALBE."""
    conn = _get_conn()
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT INTO sso_user_mapping "
            "(keycloak_sub, palbe_user_id, keycloak_username, keycloak_email, linked_at, linked_by) "
            "VALUES (?,?,?,?,?,?)",
            (keycloak_sub, palbe_user_id, keycloak_username, keycloak_email,
             now, linked_by),
        )
    conn.close()


def recordar_nombre_keycloak(keycloak_sub: str, nombre: str) -> None:
    """Guarda el nombre de persona que manda Keycloak, en cada entrada por SSO.

    Se actualiza al ENTRAR y no al vincular porque vincular lo hace una
    persona a mano una sola vez, asi que una columna rellenada solo ahi se
    quedaria vacia para todos los que ya estaban. Al hacerlo en el login, se
    llena sola la proxima vez que cada uno entre.

    Es una escritura, y es la unica del camino de SSO que no existia: se anade
    a conciencia. NO la hace la ruta de contexto ni la de activos, que siguen
    siendo de solo lectura."""
    limpio = (nombre or "").strip()
    if not keycloak_sub or not limpio:
        return
    conn = _get_conn()
    with conn:
        conn.execute(
            "UPDATE sso_user_mapping SET keycloak_name=? WHERE keycloak_sub=?",
            (limpio[:120], keycloak_sub),
        )
    conn.close()


def subs_por_usuario() -> Dict[int, Dict[str, str]]:
    """Del id interno al sub de Keycloak, con el nombre resuelto.

    Para el bloque "Trabajando ahora" del recibidor: `_USER_LAST_SEEN` de
    app_palbe_4 esta indexado por el id interno, y el recibidor necesita el
    `sub` -- es lo que le permite agrupar a la misma persona en varias
    herramientas.

    Aditiva y de solo lectura. No se amplia `list_sso_mappings` porque esa
    devuelve una LISTA ordenada por nombre para el panel de admin, y aqui hace
    falta un indice por id.
    """
    conn = _get_conn()
    filas = conn.execute(
        "SELECT m.palbe_user_id, m.keycloak_sub, u.username, "
        "       COALESCE(m.keycloak_name, '') AS nombre_keycloak "
        "FROM sso_user_mapping m JOIN users u ON u.id = m.palbe_user_id"
    ).fetchall()
    conn.close()
    # El nombre de persona si Keycloak lo ha mandado alguna vez; el usuario de
    # PALBE si no. Asi la misma persona sale igual aqui y en el recibidor.
    return {int(f[0]): {"sub": f[1], "nombre": (f[3] or "").strip() or f[2]}
            for f in filas}


def list_sso_mappings() -> List[Dict[str, Any]]:
    """Todos los mapeos, con el username de PALBE resuelto."""
    conn = _get_conn()
    filas = conn.execute(
        "SELECT m.keycloak_sub, m.keycloak_username, u.username, m.linked_at "
        "FROM sso_user_mapping m JOIN users u ON u.id = m.palbe_user_id "
        "ORDER BY u.username"
    ).fetchall()
    conn.close()
    return [
        {"keycloak_sub": f[0], "keycloak_username": f[1],
         "palbe_username": f[2], "linked_at": f[3]}
        for f in filas
    ]


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

init_db()
