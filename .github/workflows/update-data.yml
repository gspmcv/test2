name: Update operational data

on:
  schedule:
    # ECMWF en este proyecto se usa con PASO_H=3, así que actualizamos cada 3 horas.
    - cron: "20 */3 * * *"
  workflow_dispatch:

permissions:
  contents: write

jobs:
  update-data:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11.9"

      - name: Install system dependencies
        run: |
          sudo apt-get update
          sudo apt-get install -y libeccodes0

      - name: Install Python dependencies
        run: pip install -r requirements.txt

      - name: Generate latest operational data
        run: python scripts/update_data.py

      - name: Commit processed data
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data/processed/latest.json data/processed/nivel_total_ultimo.csv data/processed/forcing_ecmwf_ultimo.csv
          git commit -m "Update operational data" || echo "No processed data changes"
          git push
