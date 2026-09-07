#!/usr/bin/env python3
"""Baixa legendas de uma playlist pública do YouTube e gera arquivos .txt.

Arquivo para ser salvo na raiz do repositório LIVES como:
transcrever_lives.py
"""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
from pathlib import Path


def limpar_legenda(texto: str) -> str:
    texto = re.sub(r"<[^>]+>", "", texto)
    return html.unescape(texto).strip()


def converter_vtt_para_txt(vtt: Path) -> Path:
    linhas_saida: list[str] = []
    ultima = ""

    for linha_bruta in vtt.read_text(encoding="utf-8", errors="ignore").splitlines():
        linha = linha_bruta.strip()
        if not linha or linha == "WEBVTT" or "-->" in linha or linha.isdigit():
            continue
        if linha.startswith(("NOTE", "STYLE", "REGION", "Kind:", "Language:")):
            continue

        linha = limpar_legenda(linha)
        if linha and linha != ultima:
            linhas_saida.append(linha)
            ultima = linha

    destino = vtt.with_suffix(".txt")
    destino.write_text("\n".join(linhas_saida) + "\n", encoding="utf-8")
    vtt.unlink()
    return destino


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcreve legendas públicas de uma playlist do YouTube.")
    parser.add_argument("--playlist-url", default=os.getenv("PLAYLIST_URL"))
    parser.add_argument("--output-dir", default=os.getenv("OUTPUT_DIR", "transcricoes"))
    parser.add_argument("--language", default=os.getenv("TRANSCRIPT_LANGUAGE", "pt.*"))
    args = parser.parse_args()

    if not args.playlist_url:
        print("Erro: configure PLAYLIST_URL em Settings > Secrets and variables > Actions > Variables.", file=sys.stderr)
        return 1

    # Validar que yt-dlp está instalado
    try:
        import yt_dlp
    except ImportError:
        print("Erro: yt-dlp não está instalado corretamente.", file=sys.stderr)
        return 1

    workspace = Path(os.getenv("GITHUB_WORKSPACE", Path.cwd())).resolve()
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = workspace / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    temp_dir = Path(os.getenv("RUNNER_TEMP", "/tmp")) / "transcritor_youtube"
    temp_dir.mkdir(parents=True, exist_ok=True)
    archive_file = temp_dir / "videos_processados.txt"

    print(f"Diretório do repositório: {workspace}")
    print(f"Arquivo em execução: {Path(__file__).resolve()}")
    print(f"Diretório de saída: {output_dir}")

    comando = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-errors",
        "--no-abort-on-error",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        f"{args.language},en.*",
        "--sub-format",
        "vtt",
        "--download-archive",
        str(archive_file),
        "--output",
        str(output_dir / "%(playlist_index)03d - %(title)s [%(id)s].%(ext)s"),
        # Adicionar opções para evitar detecção de bot
        "--no-warnings",
        "-f",
        "best",
        # Usar nodejs como runtime JavaScript
        "--js-runtimes",
        "nodejs",
        # Adicionar headers para parecer com um navegador
        "--add-header",
        "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        # Aumentar timeouts
        "--socket-timeout",
        "30",
        args.playlist_url,
    ]

    resultado = subprocess.run(comando, capture_output=True, text=True, check=False)
    if resultado.returncode != 0:
        print("Erro: yt-dlp não conseguiu processar a playlist.", file=sys.stderr)
        print(f"Código de erro: {resultado.returncode}", file=sys.stderr)
        if resultado.stderr:
            print(f"Saída de erro: {resultado.stderr}", file=sys.stderr)
        return resultado.returncode

    vtts = sorted(output_dir.glob("*.vtt"))
    if not vtts:
        print("Nenhuma legenda foi encontrada. A playlist pode ser privada ou os vídeos podem não ter legendas.", file=sys.stderr)
        return 2

    for vtt in vtts:
        txt = converter_vtt_para_txt(vtt)
        print(f"Transcrição criada: {txt.relative_to(workspace)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
