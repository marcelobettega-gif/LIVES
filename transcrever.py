#!/usr/bin/env python3
"""Transcreve os vídeos de uma playlist sem criar uma pasta lives dentro do repositório."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        capture_output=capture,
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--playlist-url", default=os.getenv("PLAYLIST_URL"))
    parser.add_argument("--output-dir", default=os.getenv("OUTPUT_DIR", "transcricoes"))
    parser.add_argument("--language", default=os.getenv("TRANSCRIPT_LANGUAGE", "pt"))
    args = parser.parse_args()

    if not args.playlist_url:
        print("Erro: PLAYLIST_URL não foi configurada.", file=sys.stderr)
        return 1

    # GITHUB_WORKSPACE é a raiz do repositório no runner.
    workspace = Path(os.getenv("GITHUB_WORKSPACE", Path.cwd())).resolve()
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = workspace / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Cria uma lista temporária fora do repositório; não usa lives/lives.
    temp_dir = Path(os.getenv("RUNNER_TEMP", "/tmp")) / "transcritor-youtube"
    temp_dir.mkdir(parents=True, exist_ok=True)
    archive = temp_dir / "baixados.txt"

    print(f"Repositório: {workspace}")
    print(f"Saída: {output_dir}")
    print(f"Playlist: {args.playlist_url}")

    download_cmd = [
        "yt-dlp",
        "--ignore-errors",
        "--no-abort-on-error",
        "--write-auto-subs",
        "--sub-langs", f"{args.language},en",
        "--sub-format", "vtt",
        "--skip-download",
        "--download-archive", str(archive),
        "--output", str(output_dir / "%(playlist_index)03d - %(title)s [%(id)s].%(ext)s"),
        args.playlist_url,
    ]

    result = run(download_cmd)
    if result.returncode != 0:
        print("Erro ao consultar a playlist com yt-dlp.", file=sys.stderr)
        return result.returncode

    vtt_files = sorted(output_dir.glob("*.vtt"))
    if not vtt_files:
        print("Nenhuma legenda automática foi encontrada.", file=sys.stderr)
        print("Verifique se a playlist é pública e se o yt-dlp está atualizado.", file=sys.stderr)
        return 2

    # Converte VTT para TXT, removendo cabeçalho, marcações e timestamps.
    for vtt_file in vtt_files:
        txt_file = vtt_file.with_suffix(".txt")
        lines: list[str] = []
        previous = ""
        for raw in vtt_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
                continue
            if line.startswith(("NOTE", "STYLE", "REGION")):
                continue
            line = line.replace("<c>", "").replace("</c>", "")
            if line != previous:
                lines.append(line)
                previous = line
        txt_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        vtt_file.unlink()
        print(f"Criado: {txt_file.relative_to(workspace)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
