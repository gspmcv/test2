
import os
import json
import time
import math
import uuid
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Madrid")

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, render_template

try:
    from scipy.interpolate import PchipInterpolator
except Exception:
    PchipInterpolator = None

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ASTRO_PATH = DATA_DIR / "astro" / "astro_CAMINO_A_2026_2050_CeroREDMAR.csv"
ECMWF_CACHE_DIR = DATA_DIR / "cache" / "ecmwf"
PORTUS_CACHE_DIR = DATA_DIR / "cache" / "portus"
PROCESSED_DIR = DATA_DIR / "processed"

for d in [ECMWF_CACHE_DIR, PORTUS_CACHE_DIR, PROCESSED_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ----------------- CONFIGURACIÓN OPERATIVA -----------------
PUNTO_LAT = float(os.getenv("PUNTO_LAT", "39.55"))
PUNTO_LON = float(os.getenv("PUNTO_LON", "2.63"))

HORIZONTE_FC_H = int(os.getenv("HORIZONTE_FC_H", "120"))
PASO_H = int(os.getenv("PASO_H", "3"))
N_DIAS_HISTORICO = int(os.getenv("N_DIAS_HISTORICO", "3"))

A_U = float(os.getenv("A_U", "0.0"))
A_V = float(os.getenv("A_V", "3.5e-4"))
K_IB = float(os.getenv("K_IB", "0.93"))
ALPHA = float(os.getenv("ALPHA", "0.9"))
P_REF = float(os.getenv("P_REF", "1015.5"))
COTA_GALIBO_ENTRADA_M = float(os.getenv("COTA_GALIBO_ENTRADA_M", "1.483"))
COTA_GALIBO_SALIDA_M = float(os.getenv("COTA_GALIBO_SALIDA_M", "1.474"))

PORTUS_API_BASE = "https://poem.puertos.es/portus/ObservedHourlyLevel"
PORTUS_STATION_CODE = 3851
DATUM_OFFSET_MM = -107

JOBS = {}
LOCK = threading.Lock()


def set_job(job_id, **kwargs):
    with LOCK:
        JOBS.setdefault(job_id, {}).update(kwargs)


def get_job(job_id):
    with LOCK:
        return dict(JOBS.get(job_id, {}))


def stage(job_id, progress, message):
    set_job(job_id, progress=progress, message=message)
    time.sleep(0.2)


def utc_now_floor():
    return datetime.now(LOCAL_TZ).replace(minute=0, second=0, microsecond=0, tzinfo=None)


# ----------------- ECMWF -----------------
def descargar_ecmwf(job_id):
    """
    Descarga el último run ECMWF Open Data.
    Si hay problemas con cfgrib/ecCodes o red, usa el último CSV procesado disponible.
    """
    stage(job_id, 10, "Consultando último run ECMWF disponible...")

    try:
        from ecmwf.opendata import Client
        import cfgrib
        import xarray as xr
    except Exception as e:
        raise RuntimeError(f"No están disponibles ecmwf-opendata/cfgrib/xarray: {e}")

    mirrors = ["aws", "azure", "ecmwf"]
    latest = None
    mirror_ok = None

    for mirror in mirrors:
        try:
            client = Client(source=mirror)
            latest = client.latest(stream="oper", type="fc", param=["msl", "10u", "10v"], step=0)
            mirror_ok = mirror
            break
        except Exception:
            continue

    if latest is None:
        raise RuntimeError("No se pudo obtener el último run ECMWF.")

    fecha_run = latest.strftime("%Y%m%d")
    hora_run = latest.strftime("%H%M")
    steps = list(range(0, HORIZONTE_FC_H + 1, PASO_H))

    rows = []
    lon_ecmwf = PUNTO_LON % 360
    client = Client(source=mirror_ok)

    for idx, step in enumerate(steps):
        pct = 12 + int(35 * (idx + 1) / len(steps))
        stage(job_id, pct, f"Descargando ECMWF step {step}h...")

        grib = ECMWF_CACHE_DIR / f"ecmwf_{fecha_run}_{hora_run}_step{step:03d}.grib2"
        if not grib.exists() or grib.stat().st_size < 100_000:
            client.retrieve(
                date=int(fecha_run),
                time=int(hora_run),
                step=step,
                stream="oper",
                type="fc",
                param=["msl", "10u", "10v"],
                target=str(grib),
            )

        ds = xr.open_dataset(grib, engine="cfgrib", backend_kwargs={"indexpath": ""})
        p = ds.sel(latitude=PUNTO_LAT, longitude=lon_ecmwf, method="nearest")
        
        print("Variables ECMWF:", list(p.data_vars))
        
        def get_var(ds, names):
            for name in names:
                if name in ds.data_vars:
                    return ds[name]
            raise RuntimeError(
                f"No encuentro ninguna variable de {names}. "
                f"Disponibles: {list(ds.data_vars)}"
            )
        
        msl = get_var(p, ["msl", "prmsl"])
        u10 = get_var(p, ["u10", "10u"])
        v10 = get_var(p, ["v10", "10v"])
        
        t_valid = pd.Timestamp(p.valid_time.values).to_pydatetime().replace(tzinfo=None)
        
        rows.append({
            "time": t_valid,
            "msl_hPa": float(msl.values) / 100.0,
            "u10": float(u10.values),
            "v10": float(v10.values),
            "fuente": "forecast",
            "lat_grid": float(p.latitude.values),
            "lon_grid": float(
                p.longitude.values
                if p.longitude.values <= 180
                else p.longitude.values - 360
            ),
        })
    df = pd.DataFrame(rows).sort_values("time").drop_duplicates("time").reset_index(drop=True)
    df["wind_speed"] = np.sqrt(df.u10**2 + df.v10**2)

    out = PROCESSED_DIR / "forcing_ecmwf_ultimo.csv"
    df.to_csv(out, index=False)
    return df


# ----------------- MODELO METEOROLÓGICO -----------------
def calcular_modelo_meteo(job_id, df3):
    stage(job_id, 52, "Interpolando presión y viento a paso horario...")

    df3 = df3.sort_values("time").reset_index(drop=True)
    t_start = df3.time.iloc[0]
    t_end = df3.time.iloc[-1]
    t_h = pd.date_range(start=t_start, end=t_end, freq="1h")

    if PchipInterpolator is not None and len(df3) >= 3:
        t_sec_3 = (df3.time - t_start).dt.total_seconds().values
        t_sec_1 = (t_h - t_start).total_seconds().values
        df = pd.DataFrame({
            "time": t_h,
            "msl_hPa": PchipInterpolator(t_sec_3, df3.msl_hPa.values)(t_sec_1),
            "u10": PchipInterpolator(t_sec_3, df3.u10.values)(t_sec_1),
            "v10": PchipInterpolator(t_sec_3, df3.v10.values)(t_sec_1),
        })
    else:
        tmp = df3.set_index("time")[["msl_hPa", "u10", "v10"]]
        df = tmp.reindex(t_h).interpolate(method="time").reset_index().rename(columns={"index": "time"})

    stage(job_id, 60, "Calculando marea meteorológica η_met...")

    df["wind_speed"] = np.sqrt(df.u10**2 + df.v10**2)
    df["eta_IB"] = -0.00995 * K_IB * (df["msl_hPa"] - P_REF)
    df["eta_wind"] = A_U * df.u10 * np.abs(df.u10) + A_V * df.v10 * np.abs(df.v10)
    df["eta_inst"] = df["eta_IB"] + df["eta_wind"]

    eta = np.zeros(len(df))
    eta[0] = df["eta_inst"].iloc[0]
    for i in range(1, len(df)):
        eta[i] = ALPHA * eta[i - 1] + (1 - ALPHA) * df["eta_inst"].iloc[i]
    df["eta_met"] = eta

    for c in ["eta_IB", "eta_wind", "eta_inst", "eta_met"]:
        df[c + "_cm"] = df[c] * 100

    return df


# ----------------- ASTRONÓMICA -----------------
def leer_astro(job_id):
    stage(job_id, 68, "Leyendo CSV de marea astronómica...")

    if not ASTRO_PATH.exists():
        raise FileNotFoundError(f"No existe {ASTRO_PATH}")

    raw = pd.read_csv(ASTRO_PATH)
    if raw.shape[1] < 2:
        raise RuntimeError("El CSV de astronómica debe tener al menos 2 columnas: fecha y valor.")

    col_time = raw.columns[0]
    col_val = raw.columns[1]

    # Intenta el formato del script original y si no, parseo flexible.
    try:
        times = pd.to_datetime(raw[col_time], format="%d-%b-%Y %H:%M:%S")
    except Exception:
        times = pd.to_datetime(raw[col_time], errors="coerce", dayfirst=True)

    vals = pd.to_numeric(raw[col_val], errors="coerce")

    df = pd.DataFrame({
        "time": times,
        "astro_m": vals,
    }).dropna()

    df["time"] = pd.to_datetime(df["time"]).dt.tz_localize(None)
    df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    df["astro_cm"] = df["astro_m"] * 100
    return df


# ----------------- PORTUS -----------------
def cargar_mareografo(job_id, fecha_fin, n_dias=3):
    stage(job_id, 76, "Descargando mareógrafo PORTUS Palma 3851...")

    fecha_ini = fecha_fin - timedelta(days=n_dias - 1)
    t_from = fecha_ini.strftime("%Y%m%d") + "@0000"
    t_to = fecha_fin.strftime("%Y%m%d") + "@2300"
    cache = PORTUS_CACHE_DIR / f"portus_3851_{t_from}_{t_to}.json"

    if cache.exists() and (time.time() - cache.stat().st_mtime) < 3600:
        data = json.loads(cache.read_text(encoding="utf-8"))
    else:
        url = (
            f"{PORTUS_API_BASE}?fields=Datetime,SeaLevel,Residual"
            f"&code={PORTUS_STATION_CODE}&from={t_from}&to={t_to}"
        )
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        cache.write_text(json.dumps(data), encoding="utf-8")

    if not data:
        return pd.DataFrame(columns=["time", "sealevel_m", "sealevel_cm", "residual_m", "residual_cm"])

    arr = np.asarray(data, dtype=float)
    if arr.ndim != 2 or arr.shape[1] != 3:
        return pd.DataFrame(columns=["time", "sealevel_m", "sealevel_cm", "residual_m", "residual_cm"])

    mm_api = arr[:, 1]
    mm_redmar = mm_api + DATUM_OFFSET_MM
    mm_resid = arr[:, 2]

    df = pd.DataFrame({
        "time": pd.to_datetime(arr[:, 0], unit="s", utc=True).tz_localize(None),
        "sealevel_m": mm_redmar / 1000.0,
        "sealevel_cm": mm_redmar / 10.0,
        "residual_m": mm_resid / 1000.0,
        "residual_cm": mm_resid / 10.0,
    })
    df = df[np.isfinite(df["sealevel_cm"])]
    return df.sort_values("time").drop_duplicates("time").reset_index(drop=True)


# ----------------- UNIÓN Y RESUMEN -----------------
def combinar(job_id, df_meteo, df_astro, df_mareo):
    stage(job_id, 84, "Combinando meteo + astronómica...")

    df = df_meteo.merge(df_astro, on="time", how="inner").sort_values("time").reset_index(drop=True)
    if df.empty:
        raise RuntimeError("No hay solape temporal entre ECMWF horario y el CSV astronómico.")

    ahora = utc_now_floor()
    inicio_hist = ahora - timedelta(days=N_DIAS_HISTORICO)

    df = df[df.time >= inicio_hist].reset_index(drop=True)
    df["fuente"] = np.where(df["time"] <= ahora, "historico", "forecast")
    df["total_m"] = df["eta_met"] + df["astro_m"]
    df["total_cm"] = df["total_m"] * 100
    df["galibo_entrada_m"] = COTA_GALIBO_ENTRADA_M - df["total_m"]
    df["galibo_entrada_cm"] = df["galibo_entrada_m"] * 100
    
    df["galibo_salida_m"] = COTA_GALIBO_SALIDA_M - df["total_m"]
    df["galibo_salida_cm"] = df["galibo_salida_m"] * 100
    
    df["residuo_m"] = df["eta_met"]
    df["residuo_cm"] = df["eta_met_cm"]

    csv_out = PROCESSED_DIR / "nivel_total_ultimo.csv"
    df.to_csv(csv_out, index=False)

    stage(job_id, 92, "Preparando gráfico y tabla...")

    now_row = df.iloc[(df.time - ahora).abs().argsort()[:1]]
    current = now_row.iloc[0] if len(now_row) else df.iloc[0]

    # extremos simples próximos
    dd = df.copy()
    dd["d"] = dd["total_cm"].diff()
    highs = []
    lows = []
    for i in range(1, len(dd) - 1):
        if dd.total_cm.iloc[i] >= dd.total_cm.iloc[i-1] and dd.total_cm.iloc[i] >= dd.total_cm.iloc[i+1] and dd.time.iloc[i] >= ahora:
            highs.append(dd.iloc[i])
        if dd.total_cm.iloc[i] <= dd.total_cm.iloc[i-1] and dd.total_cm.iloc[i] <= dd.total_cm.iloc[i+1] and dd.time.iloc[i] >= ahora:
            lows.append(dd.iloc[i])

    next_high = highs[0] if highs else None
    next_low = lows[0] if lows else None

    def clean(v, nd=1):
        if pd.isna(v) or not np.isfinite(v):
            return None
        return round(float(v), nd)

    result = {
        "generated_at": datetime.now(LOCAL_TZ).strftime("%d-%m-%Y %H:%M Madrid"),
        "period": {
            "start": df.time.iloc[0].strftime("%d-%m-%Y %H:%M"),
            "end": df.time.iloc[-1].strftime("%d-%m-%Y %H:%M"),
        },
        "cards": {
            "total_cm": clean(current.total_cm),
            "astro_cm": clean(current.astro_cm),
            "meteo_cm": clean(current.eta_met_cm),
            "galibo_entrada_m": clean(current.galibo_entrada_m,3),
            "galibo_salida_m": clean(current.galibo_salida_m,3),
            "next_high_time": next_high.time.strftime("%d-%m %Hh") if next_high is not None else "—",
            "next_high_cm": clean(next_high.total_cm) if next_high is not None else None,
            "next_low_time": next_low.time.strftime("%d-%m %Hh") if next_low is not None else "—",
            "next_low_cm": clean(next_low.total_cm) if next_low is not None else None,
        },
        "series": {
            "time": [t.strftime("%d-%m %Hh") for t in df.time],
            "iso": [t.strftime("%Y-%m-%d %H:%M") for t in df.time],
            "total_cm": [clean(v) for v in df.total_cm],
            "astro_cm": [clean(v) for v in df.astro_cm],
            "meteo_cm": [clean(v) for v in df.eta_met_cm],
            "galibo_entrada_m": [clean(v,3) for v in df.galibo_entrada_m],
            "galibo_salida_m": [clean(v,3) for v in df.galibo_salida_m],
            "fuente": df.fuente.tolist(),
            "mareo_time": [t.strftime("%d-%m %Hh") for t in df_mareo.time] if not df_mareo.empty else [],
            "mareo_cm": [clean(v) for v in df_mareo.sealevel_cm] if not df_mareo.empty else [],
        },
        "table": [
            {
                "fecha": r.time.strftime("%d-%m %Hh"),
                "total": clean(r.total_cm),
                "astro": clean(r.astro_cm),
                "meteo": clean(r.eta_met_cm),
                "galibo_entrada": clean(r.galibo_entrada_m, 3),
                "galibo_salida": clean(r.galibo_salida_m, 3),
                "fuente": r.fuente,
            }
            for _, r in df.head(160).iterrows()
        ],
    }
    return result


def latest_json_path():
    return PROCESSED_DIR / "latest.json"


def load_latest_result():
    path = latest_json_path()
    if not path.exists():
        raise FileNotFoundError(
            "No existe data/processed/latest.json. "
            "Ejecuta primero la GitHub Action 'Update operational data'."
        )
    return json.loads(path.read_text(encoding="utf-8"))

def with_current_cards(result):
    now = datetime.now(LOCAL_TZ).replace(minute=0, second=0, microsecond=0)
    now_label = now.strftime("%d-%m %Hh")

    times = result["series"]["time"]

    if now_label in times:
        i = times.index(now_label)
    else:
        # fallback: si no coincide exacto, coge el punto más cercano
        iso_times = [
            datetime.strptime(t, "%Y-%m-%d %H:%M")
            for t in result["series"].get("iso", [])
        ]
        i = min(range(len(iso_times)), key=lambda k: abs(iso_times[k] - now.replace(tzinfo=None)))

    result["cards"]["total_cm"] = result["series"]["total_cm"][i]
    result["cards"]["astro_cm"] = result["series"]["astro_cm"][i]
    result["cards"]["meteo_cm"] = result["series"]["meteo_cm"][i]
    result["cards"]["galibo_entrada_m"] = result["series"]["galibo_entrada_m"][i]
    result["cards"]["galibo_salida_m"] = result["series"]["galibo_salida_m"][i]

    mareo_times = result["series"].get("mareo_time", [])
    mareo_vals = result["series"].get("mareo_cm", [])
    if now_label in mareo_times:
        result["cards"]["portus_cm"] = mareo_vals[mareo_times.index(now_label)]

    result["cards"]["current_time"] = now_label
    return result


def save_latest_result(result):
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    latest_json_path().write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def limpiar_cache_antigua(max_age_hours=36):
    """
    Borra solo archivos temporales antiguos de caché.
    No borra data/processed/latest.json ni los CSV finales que usa la web.
    """
    cutoff = time.time() - max_age_hours * 3600
    for folder in [ECMWF_CACHE_DIR, PORTUS_CACHE_DIR]:
        if not folder.exists():
            continue
        for f in folder.rglob("*"):
            if f.is_file() and f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                except OSError:
                    pass


def build_operational_dataset(job_id="github-action"):
    """
    Ejecuta el cálculo pesado.
    Esto lo debe llamar GitHub Actions cada 3 horas, no Render por cada visita.
    """
    set_job(job_id, status="running", progress=2, message="Arrancando cálculo operativo...")

    df_ecmwf = descargar_ecmwf(job_id)
    df_meteo = calcular_modelo_meteo(job_id, df_ecmwf)
    df_astro = leer_astro(job_id)

    hoy = datetime.now(LOCAL_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )

    try:
        df_mareo = cargar_mareografo(job_id, hoy, n_dias=3)
    except Exception as e:
        print(f"PORTUS no disponible: {e}")
        df_mareo = pd.DataFrame(
            columns=["time", "sealevel_m", "sealevel_cm", "residual_m", "residual_cm"]
        )

    result = combinar(job_id, df_meteo, df_astro, df_mareo)
    save_latest_result(result)
    limpiar_cache_antigua(max_age_hours=int(os.getenv("CACHE_MAX_AGE_HOURS", "36")))
    set_job(job_id, status="done", progress=100, message="Listo.", result=result)
    return result


def run_job(job_id):
    """
    Compatibilidad local/manual: permite regenerar datos si se llama explícitamente.
    La web desplegada en Render no usa esta función para evitar capar ECMWF.
    """
    try:
        result = build_operational_dataset(job_id)
        set_job(job_id, status="done", progress=100, message="Listo.", result=result)
    except Exception as e:
        set_job(job_id, status="error", progress=100, message=str(e), error=str(e))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def api_start():
    """
    La web conserva el mismo flujo visual, pero ya no descarga ECMWF.
    Crea un job instantáneo leyendo el último resultado precalculado por GitHub Actions.
    """
    job_id = uuid.uuid4().hex
    try:
        result = with_current_cards(load_latest_result())
        set_job(job_id, status="done", progress=100, message="Datos cargados.", result=result)
    except Exception as e:
        set_job(job_id, status="error", progress=100, message=str(e), error=str(e))
    return jsonify({"job_id": job_id})


@app.route("/api/latest")
def api_latest():
    try:
        return jsonify(with_current_cards(load_latest_result()))
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"status": "missing", "message": "Trabajo no encontrado."}), 404
    return jsonify(job)


@app.route("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, threaded=True)
