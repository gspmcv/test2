"""
Actualiza los datos operativos para Portus Total Operativo.

Este script está pensado para ejecutarse desde GitHub Actions cada 3 horas.
Render no debe llamar a ECMWF por cada visita: la web solo lee data/processed/latest.json.
"""
from app import build_operational_dataset

if __name__ == "__main__":
    build_operational_dataset("github-action")
