# Portus Total Operativo

Aplicación Flask lista para abrir en VS Code.

Al entrar en la web lanza automáticamente:
1. Descarga ECMWF IFS Open Data.
2. Extrae presión y viento para Palma.
3. Interpola a horario con PCHIP.
4. Calcula marea meteorológica.
5. Lee la marea astronómica desde `data/astro/astro_CAMINO_A_2026_2050_CeroREDMAR.csv`.
6. Descarga PORTUS 3851.
7. Calcula nivel total y muestra tarjetas, gráfico y tabla.

## Local

```bash
pip install -r requirements.txt
python app.py
```

Abre:

```text
http://127.0.0.1:5000
```

## Render

Sube el repo completo a GitHub y conecta Render. El `render.yaml` ya está incluido.

## Nota

La primera carga puede tardar porque descarga GRIBs ECMWF y consulta PORTUS.

## Actualización operativa con GitHub Actions

La web en Render no descarga ECMWF por cada visita. El flujo operativo es:

1. GitHub Actions ejecuta `scripts/update_data.py` cada 3 horas.
2. El script descarga ECMWF con `PASO_H=3`, recalcula el resultado y guarda `data/processed/latest.json`.
3. Render sirve `/api/start` leyendo ese `latest.json`, manteniendo la misma interfaz visual.
4. La caché temporal antigua se limpia automáticamente, pero no se borra el resultado procesado que usa la web.

Para generar el primer `latest.json`, entra en GitHub → Actions → `Update operational data` → `Run workflow`.
