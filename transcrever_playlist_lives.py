#!/usr/bin/env python3
"""Transcreve todos os vídeos de uma playlist em ordem cronológica.

Para 20 vídeos, gera:
  transcricoes/01 - transcrição do vídeo Título da live.txt
  ...
  transcricoes/20 - transcrição do vídeo Título da live.txt

Uso:
  pip install -r requirements.txt
  python transcrever_playlist_lives.py --playlist-url "https://youtube.com/playlist?list=..."

Defina YOUTUBE_API_KEY como variável de ambiente para usar a YouTube Data API.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from youtube_transcript_api import YouTubeTranscriptApi

TRANSCRIPTS_DIR = Path("transcricoes")
ERRORS_DIR = Path("erros")


@dataclass(frozen=True)
class Video:
    video_id: str
    title: str
    published_at: datetime

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


def playlist_id_from_url(url: str) -> str:
    match = re.search(r"(?:[?&]list=|/playlist/)([A-Za-z0-9_-]+)", url)
    if not match:
        raise ValueError("Não foi possível identificar 'list' na URL da playlist.")
    return match.group(1)


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def videos_com_youtube_api(playlist_id: str, api_key: str) -> list[Video]:
    from googleapiclient.discovery import build

    youtube = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    video_ids: list[str] = []
    page_token: str | None = None

    while True:
        page = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        video_ids.extend(
            item["contentDetails"]["videoId"]
            for item in page.get("items", [])
            if item.get("contentDetails", {}).get("videoId")
        )
        page_token = page.get("nextPageToken")
        if not page_token:
            break

    videos: list[Video] = []
    for start in range(0, len(video_ids), 50):
        page = youtube.videos().list(
            part="snippet,status",
            id=",".join(video_ids[start : start + 50]),
            maxResults=50,
        ).execute()
        for item in page.get("items", []):
            if item.get("status", {}).get("privacyStatus") != "public":
                continue
            snippet = item.get("snippet", {})
            if snippet.get("publishedAt"):
                videos.append(
                    Video(
                        video_id=item["id"],
                        title=snippet.get("title", "Sem título"),
                        published_at=parse_datetime(snippet["publishedAt"]),
                    )
                )
    return videos


def videos_com_ytdlp(playlist_url: str) -> list[Video]:
    import yt_dlp

    options: dict[str, Any] = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": False,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        playlist = ydl.extract_info(playlist_url, download=False)

    videos: list[Video] = []
    for item in (playlist or {}).get("entries", []):
        if not item or not item.get("id"):
            continue
        upload_date = item.get("upload_date")
        timestamp = item.get("timestamp") or item.get("release_timestamp")
        if upload_date:
            published_at = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        elif timestamp:
            published_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            continue
        videos.append(
            Video(
                video_id=item["id"],
                title=item.get("title") or "Sem título",
                published_at=published_at,
            )
        )
    return videos


def safe_title(title: str, max_length: int = 150) -> str:
    title = unicodedata.normalize("NFC", title).strip()
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", title)
    title = re.sub(r"\s+", " ", title)
    return (title[:max_length].rstrip(". ") or "Sem título")


def output_path(position: int, total: int, video: Video) -> Path:
    digits = max(2, len(str(total)))
    return TRANSCRIPTS_DIR / f"{position:0{digits}d} - transcrição do vídeo {safe_title(video.title)}.txt"


def prior_output(video: Video) -> Path | None:
    marker = f"URL: {video.url}"
    for file in TRANSCRIPTS_DIR.glob("*.txt"):
        try:
            if marker in file.read_text(encoding="utf-8", errors="ignore"):
                return file
        except OSError:
            pass
    return None


def choose_transcript(video_id: str):
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)
    try:
        return transcript_list.find_transcript(["pt", "pt-BR", "en"])
    except Exception:
        for transcript in transcript_list:
            if transcript.is_translatable:
                return transcript.translate("pt")
        raise RuntimeError("Nenhuma legenda utilizável ou traduzível foi encontrada.")


def save_transcript(video: Video, position: int, total: int) -> Path:
    transcript = choose_transcript(video.video_id)
    pieces = transcript.fetch()
    content = [
        f"Título: {video.title}",
        f"URL: {video.url}",
        f"Data de publicação: {video.published_at:%d/%m/%Y}",
        f"Posição cronológica na playlist: {position}/{total}",
        f"Idioma: {getattr(transcript, 'language_code', 'desconhecido')}",
        "",
    ]
    content.extend(piece.text.strip() for piece in pieces if piece.text and piece.text.strip())

    target = output_path(position, total, video)
    target.write_text("\n".join(content) + "\n", encoding="utf-8")
    return target


def save_error(video: Video, position: int, total: int, exc: Exception) -> Path:
    target = ERRORS_DIR / f"{position:02d} - erro - {video.video_id}.txt"
    target.write_text(
        "\n".join(
            [
                f"Título: {video.title}",
                f"URL: {video.url}",
                f"Data de publicação: {video.published_at:%d/%m/%Y}",
                f"Posição cronológica na playlist: {position}/{total}",
                f"Motivo: {type(exc).__name__}: {exc}",
                "",
                traceback.format_exc(),
            ]
        ),
        encoding="utf-8",
    )
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transcreve todos os vídeos pendentes, do mais antigo ao mais novo."
    )
    parser.add_argument("--playlist-url", required=True)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--retry-errors", action="store_true")
    args = parser.parse_args()

    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    ERRORS_DIR.mkdir(exist_ok=True)

    try:
        api_key = os.getenv("YOUTUBE_API_KEY")
        videos = (
            videos_com_youtube_api(playlist_id_from_url(args.playlist_url), api_key)
            if api_key
            else videos_com_ytdlp(args.playlist_url)
        )
    except Exception as exc:
        print(f"Não foi possível ler a playlist: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    videos.sort(key=lambda video: (video.published_at, video.video_id))
    total = len(videos)
    if total == 0:
        print("Nenhum vídeo público com data de publicação foi encontrado.", file=sys.stderr)
        return 2

    print(f"{total} vídeos encontrados. Processando do mais antigo ao mais novo.")
    saved = skipped = failed = 0

    for position, video in enumerate(videos, start=1):
        error_path = ERRORS_DIR / f"{position:02d} - erro - {video.video_id}.txt"
        if prior_output(video):
            skipped += 1
            print(f"[{position:02d}/{total:02d}] Já processado: {video.title}")
            continue
        if error_path.exists() and not args.retry_errors:
            skipped += 1
            print(f"[{position:02d}/{total:02d}] Falha já registrada: {video.title}")
            continue

        try:
            target = save_transcript(video, position, total)
            saved += 1
            if error_path.exists():
                error_path.unlink()
            print(f"[{position:02d}/{total:02d}] Salvo: {target.name}")
        except Exception as exc:
            failed += 1
            target = save_error(video, position, total, exc)
            print(f"[{position:02d}/{total:02d}] Erro: {target.name}", file=sys.stderr)

        if position < total and args.delay > 0:
            time.sleep(args.delay)

    print(f"Concluído — salvos: {saved}; ignorados: {skipped}; erros: {failed}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
