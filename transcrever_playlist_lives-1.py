import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from youtube_transcript_api import YouTubeTranscriptApi


def playlist_id_from_url(url: str) -> str:
    playlist_id = parse_qs(urlparse(url).query).get("list", [""])[0].strip()
    if not playlist_id:
        raise ValueError("A URL informada não contém o parâmetro 'list' da playlist.")
    return playlist_id


def video_ids_from_playlist(playlist_id: str, api_key: str) -> list[str]:
    video_ids = []
    page_token = ""

    while True:
        endpoint = (
            "https://www.googleapis.com/youtube/v3/playlistItems"
            f"?part=contentDetails&maxResults=50&playlistId={playlist_id}&key={api_key}"
        )
        if page_token:
            endpoint += f"&pageToken={page_token}"

        with urlopen(endpoint, timeout=30) as response:
            data = json.load(response)

        for item in data.get("items", []):
            video_id = item.get("contentDetails", {}).get("videoId")
            if video_id:
                video_ids.append(video_id)

        page_token = data.get("nextPageToken", "")
        if not page_token:
            return video_ids


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "video"


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcreve os vídeos de uma playlist pública do YouTube.")
    parser.add_argument("--playlist-url", required=True, help="URL completa da playlist do YouTube.")
    parser.add_argument("--retry-errors", action="store_true", help="Tenta novamente vídeos que falharam em execução anterior.")
    args = parser.parse_args()

    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        print("Erro: configure o secret YOUTUBE_API_KEY no GitHub.", file=sys.stderr)
        return 2

    try:
        playlist_id = playlist_id_from_url(args.playlist_url)
        video_ids = video_ids_from_playlist(playlist_id, api_key)
    except Exception as exc:
        print(f"Erro ao carregar a playlist: {exc}", file=sys.stderr)
        return 2

    output_dir = Path("transcricoes")
    output_dir.mkdir(exist_ok=True)
    errors_file = output_dir / "erros.txt"
    previous_errors = set()

    if args.retry_errors and errors_file.exists():
        previous_errors = {line.strip() for line in errors_file.read_text(encoding="utf-8").splitlines() if line.strip()}
        if previous_errors:
            video_ids = [video_id for video_id in video_ids if video_id in previous_errors]

    if not video_ids:
        print("Nenhum vídeo encontrado para transcrever.")
        return 0

    api = YouTubeTranscriptApi()
    failures = []

    for index, video_id in enumerate(video_ids, start=1):
        output_file = output_dir / f"{index:03d}_{safe_name(video_id)}.txt"
        if output_file.exists() and not args.retry_errors:
            print(f"[{index}/{len(video_ids)}] Já existe: {video_id}")
            continue

        try:
            transcript = api.fetch(video_id, languages=["pt", "pt-BR", "en"])
            text = "\n".join(snippet.text for snippet in transcript)
            output_file.write_text(text + "\n", encoding="utf-8")
            print(f"[{index}/{len(video_ids)}] OK: {video_id}")
        except Exception as exc:
            failures.append(video_id)
            print(f"[{index}/{len(video_ids)}] Falhou: {video_id} — {exc}", file=sys.stderr)

    errors_file.write_text("\n".join(failures) + ("\n" if failures else ""), encoding="utf-8")
    print(f"Concluído. Transcrições: {output_dir}. Falhas: {len(failures)}.")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
