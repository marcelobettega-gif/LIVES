name: Transcrever live Fabio Adriano

on:
  schedule:
    - cron: "0 12 * * *"
  workflow_dispatch:

permissions:
  contents: write

jobs:
  transcrever:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Instalar dependências
        run: |
          pip install requests beautifulsoup4

      - name: Rodar transcrição
        run: python transcrever.py

      - name: Salvar resultado
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add ultima_live.txt
          git commit -m "Atualiza transcrição da última live" || exit 0
          git push
