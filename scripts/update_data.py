"""
Actualiza los datos operativos para Portus Total Operativo.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import build_operational_dataset

if __name__ == "__main__":
    build_operational_dataset("github-action")
