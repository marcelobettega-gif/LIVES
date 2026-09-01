import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright


CHANNEL_URL = "https://www.youtube.com/@BTGTrader/streams"
OUTPUT_FILE = Path("ultima_live_btg.txt")

WAIT_BETWEEN_ATTEMPTS = 45
MIN_TRANSCRIPT_LENGTH = 1800
MIN_TRANSCRIPT_WORDS = 250
MIN_TIMESTAMP_COUNT = 4

BAD_MESSAGES = (
    "transcript not available",
    "no transcript available",
    "transcript unavailable",
    "unable to generate transcript",
    "could not generate transcript",
    "video unavailable",
    "this video is unavailable",
    "captcha",
    "cloudflare",
)

PAGE_FOOTERS = (
    "Works on any YouTube video.",
    "Read another video",
    "ONE EMAIL, NO SPAM",
    "Get transcripts by email",
)


def extract_video_id(url: str):
    patterns = (
        r"/live/([A-Za-z0-9_-]{11})",
        r"[?&]v=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
    )
    for pattern in patterns:
        match = re.search(pattern, url or "")
        if match:
            return match.group(1)
    return None


def normalize_whitespace(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def count_timestamps(text: str) -> int:
    return len(
        re.findall(
            r"(?m)(?:^|\n)\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:\n|$)",
            text or "",
        )
    )


def strip_page_chrome(text: str) -> str:
    if not text:
        return ""

    text = normalize_whitespace(text)

    marker = re.search(r"(?mi)^\s*TRANSCRIPT\s*$", text)
    if marker:
        text = text[marker.start():]

    endings = []
    for footer in PAGE_FOOTERS:
        pos = text.find(footer)
        if pos >= 0:
            endings.append(pos)

    if endings:
        text = text[:min(endings)]

    junk_lines = {
        "Copy",
        "Download ▾",
        "Batch ▾",
        "NEXT VIDEO",
        "Open transcript",
        "Share",
        "Search",
        "Batch",
        "YouTube",
        "?",
        "1×",
        "×",
    }

    lines = [
        line for line in text.splitlines()
        if line.strip() not in junk_lines
    ]
    return normalize_whitespace("\n".join(lines))


def validate_transcript(text: str) -> bool:
    if not text:
        return False

    text = normalize_whitespace(text)

    if len(text) < MIN_TRANSCRIPT_LENGTH:
        return False

    words = re.findall(r"\b\w+\b", text, flags=re.UNICODE)
    if len(words) < MIN_TRANSCRIPT_WORDS:
        return False

    lower = text.lower()
    if any(message in lower for message in BAD_MESSAGES):
        return False

    if count_timestamps(text) >= MIN_TIMESTAMP_COUNT:
        return True

    has_marker = bool(re.search(r"(?mi)^\s*TRANSCRIPT\s*$", text))
    return has_marker and len(words) >= 500


def prepare_transcript(text: str):
    text = strip_page_chrome(text)
    return text if validate_transcript(text) else None


def get_saved_video_id():
    if not OUTPUT_FILE.exists():
        return None

    try:
        head = OUTPUT_FILE.read_text(
            encoding="utf-8",
            errors="ignore",
        )[:1500]
    except Exception:
        return None

    match = re.search(
        r"(?m)^VIDEO_ID:\s*([A-Za-z0-9_-]{11})\s*$",
        head,
    )
    return match.group(1) if match else None


def existing_file_is_valid_for(video_id: str) -> bool:
    if not OUTPUT_FILE.exists():
        return False

    try:
        text = OUTPUT_FILE.read_text(
            encoding="utf-8",
            errors="ignore",
        )
    except Exception:
        return False

    if f"VIDEO_ID: {video_id}" not in text[:1500]:
        return False

    parts = text.split("\n\n", 1)
    return (
        len(parts) == 2
        and validate_transcript(parts[1])
    )


def find_latest_completed_live(page):
    print("Abrindo canal BTG Trader...")

    page.goto(
        CHANNEL_URL,
        wait_until="domcontentloaded",
        timeout=90000,
    )
    page.wait_for_timeout(7000)

    print(
        "Procurando a live concluída mais recente "
        "na aba Ao vivo..."
    )

    selectors = (
        'ytd-rich-item-renderer a[href*="/live/"], '
        'ytd-rich-item-renderer a[href*="/watch?v="]',
        'a[href*="/live/"], a[href*="/watch?v="]',
    )

    seen = set()

    for selector in selectors:
        links = page.locator(selector)

        for i in range(min(links.count(), 100)):
            try:
                link = links.nth(i)
                href = link.get_attribute("href")
                video_id = extract_video_id(href or "")

                if not video_id or video_id in seen:
                    continue

                seen.add(video_id)

                card_text = ""
                title = ""

                try:
                    title = (
                        link.get_attribute("title")
                        or ""
                    ).strip()
                except Exception:
                    pass

                try:
                    card = link.locator(
                        "xpath=ancestor::ytd-rich-item-renderer[1]"
                    )

                    if card.count() > 0:
                        card_text = card.inner_text(
                            timeout=2500
                        )

                        if not title:
                            lines = [
                                x.strip()
                                for x in card_text.splitlines()
                                if x.strip()
                            ]

                            if lines:
                                title = lines[0]

                except Exception:
                    pass

                lower = card_text.lower()

                future_or_live_markers = (
                    "agendado",
                    "scheduled",
                    "estreia em",
                    "premieres",
                    "assistindo agora",
                    "watching now",
                    "ao vivo agora",
                    "live now",
                )

                if any(
                    marker in lower
                    for marker in future_or_live_markers
                ):
                    print(
                        "Ignorando live futura/em andamento: "
                        f"{video_id}"
                    )
                    continue

                youtube_url = (
                    f"https://www.youtube.com/live/{video_id}"
                )

                print(f"VIDEO_ID encontrado: {video_id}")
                print(f"TITLE: {title}")
                print(f"URL: {youtube_url}")

                return (
                    video_id,
                    youtube_url,
                    title,
                )

            except Exception as exc:
                print(
                    f"Aviso ao analisar card {i}: {exc}"
                )

    raise RuntimeError(
        "Não foi possível localizar uma live "
        "concluída no canal BTG Trader."
    )


def extract_transcript_from_page(page):
    candidates = []

    selectors = (
        '[class*="transcript"]',
        '[id*="transcript"]',
        '[class*="caption"]',
        '[id*="caption"]',
        "textarea",
        "pre",
        "article",
        "main",
    )

    for selector in selectors:
        try:
            locator = page.locator(selector)

            for i in range(
                min(locator.count(), 30)
            ):
                try:
                    element = locator.nth(i)

                    try:
                        text = element.input_value(
                            timeout=2000
                        )
                    except Exception:
                        text = element.inner_text(
                            timeout=3000
                        )

                    text = prepare_transcript(text)

                    if text:
                        candidates.append(text)

                except Exception:
                    continue

        except Exception:
            continue

    try:
        body = page.locator(
            "body"
        ).inner_text(
            timeout=5000
        )

        body = prepare_transcript(body)

        if body:
            candidates.append(body)

    except Exception:
        pass

    return (
        max(candidates, key=len)
        if candidates
        else None
    )


def try_2outube_direct(page, video_id):
    urls = (
        f"https://2outube.com/watch?v={video_id}",
        f"https://2outube.com/live/{video_id}",
    )

    for url in urls:
        try:
            print(
                f"Tentando 2outube diretamente: {url}"
            )

            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=90000,
            )

            page.wait_for_timeout(8000)

            transcript = (
                extract_transcript_from_page(page)
            )

            if transcript:
                print(
                    "Transcrição válida no 2outube "
                    f"({len(transcript)} caracteres)."
                )
                return transcript

        except Exception as exc:
            print(
                "Falha no acesso direto ao 2outube: "
                f"{exc}"
            )

    return None


def find_visible_input(page):
    selectors = (
        'input[type="url"]',
        'input[name*="url"]',
        'input[id*="url"]',
        'input[placeholder*="YouTube"]',
        'input[placeholder*="youtube"]',
        'input[placeholder*="Paste"]',
        'input[placeholder*="paste"]',
    )

    for selector in selectors:
        try:
            locator = page.locator(selector)

            for i in range(locator.count()):
                candidate = locator.nth(i)

                if candidate.is_visible():
                    return candidate

        except Exception:
            continue

    return None


def click_transcript_button(page):
    pattern = re.compile(
        r"get\s+(free\s+)?transcript"
        r"|generate\s+transcript"
        r"|transcribe"
        r"|generate"
        r"|submit",
        re.I,
    )

    try:
        buttons = page.get_by_role(
            "button",
            name=pattern,
        )

        for i in range(buttons.count()):
            candidate = buttons.nth(i)

            if candidate.is_visible():
                candidate.click(
                    timeout=15000
                )
                return True

    except Exception:
        pass

    try:
        buttons = page.locator(
            'button[type="submit"], '
            'input[type="submit"]'
        )

        for i in range(buttons.count()):
            candidate = buttons.nth(i)

            if candidate.is_visible():
                candidate.click(
                    timeout=15000
                )
                return True

    except Exception:
        pass

    return False


def try_2outube_form(page, youtube_url):
    print(
        "Tentando formulário do 2outube..."
    )

    try:
        page.goto(
            "https://2outube.com/",
            wait_until="domcontentloaded",
            timeout=90000,
        )

        page.wait_for_timeout(4000)

        input_box = find_visible_input(page)

        if input_box is None:
            print(
                "Campo de URL não encontrado "
                "no 2outube."
            )
            return None

        input_box.fill(youtube_url)

        page.wait_for_timeout(700)

        if not click_transcript_button(page):
            print(
                "Botão de transcrição não encontrado."
            )
            return None

        page.wait_for_timeout(12000)

        transcript = (
            extract_transcript_from_page(page)
        )

        if transcript:
            print(
                "Transcrição válida via formulário "
                f"({len(transcript)} caracteres)."
            )
            return transcript

    except Exception as exc:
        print(
            "Falha no formulário do 2outube: "
            f"{exc}"
        )

    return None


def try_tubetranscript(page, youtube_url):
    """Tenta gerar a transcrição no TubeTranscript em português."""
    print("Tentando TubeTranscript...")

    try:
        page.goto(
            "https://tubetranscript.com/pt/",
            wait_until="domcontentloaded",
            timeout=90000,
        )
        page.wait_for_timeout(4000)

        input_box = find_visible_input(page)
        if input_box is None:
            selectors = (
                'input[placeholder*="URL do vídeo"]',
                'input[placeholder*="URL"]',
                'input[type="text"]',
            )
            for selector in selectors:
                loc = page.locator(selector)
                for i in range(loc.count()):
                    if loc.nth(i).is_visible():
                        input_box = loc.nth(i)
                        break
                if input_box is not None:
                    break

        if input_box is None:
            print("Campo de URL não encontrado no TubeTranscript.")
            return None

        input_box.fill(youtube_url)
        page.wait_for_timeout(700)

        clicked = False
        for pattern in (
            re.compile(r"gerar\s+transcri", re.I),
            re.compile(r"generate\s+transcript", re.I),
            re.compile(r"transcri", re.I),
        ):
            try:
                buttons = page.get_by_role("button", name=pattern)
                for i in range(buttons.count()):
                    if buttons.nth(i).is_visible():
                        buttons.nth(i).click(timeout=15000)
                        clicked = True
                        break
            except Exception:
                pass
            if clicked:
                break

        if not clicked:
            try:
                submit = page.locator('button[type="submit"], input[type="submit"]')
                for i in range(submit.count()):
                    if submit.nth(i).is_visible():
                        submit.nth(i).click(timeout=15000)
                        clicked = True
                        break
            except Exception:
                pass

        if not clicked:
            print("Botão 'Gerar transcrição' não encontrado no TubeTranscript.")
            return None

        for wait_round in range(1, 13):
            page.wait_for_timeout(10000)
            transcript = extract_transcript_from_page(page)
            if transcript:
                print(
                    f"Transcrição válida no TubeTranscript ({len(transcript)} caracteres)."
                )
                return transcript
            print(f"TubeTranscript ainda processando... {wait_round * 10}s")

    except Exception as exc:
        print(f"Falha no TubeTranscript: {exc}")

    return None


def get_transcript_with_retries(page, video_id, youtube_url):
    """Alterna 2outube -> TubeTranscript até conseguir uma transcrição válida."""
    cycle = 0
    while True:
        cycle += 1
        print("")
        print("=" * 60)
        print(f"CICLO DE TRANSCRIÇÃO {cycle}: 2outube -> TubeTranscript")
        print("=" * 60)

        transcript = try_2outube_direct(page, video_id)
        if transcript:
            return transcript, "2outube-direct"

        transcript = try_2outube_form(page, youtube_url)
        if transcript:
            return transcript, "2outube-form"

        transcript = try_tubetranscript(page, youtube_url)
        if transcript:
            return transcript, "TubeTranscript"

        try:
            Path(f"debug_btg_transcript_cycle_{cycle}.html").write_text(
                page.content(), encoding="utf-8"
            )
        except Exception:
            pass

        print(
            "2outube e TubeTranscript não retornaram transcrição válida. "
            f"Novo ciclo em {WAIT_BETWEEN_ATTEMPTS} segundos..."
        )
        time.sleep(WAIT_BETWEEN_ATTEMPTS)


def save_transcript(
    video_id,
    youtube_url,
    title,
    transcript,
    source,
):
    transcript = prepare_transcript(
        transcript
    )

    if not transcript:
        raise RuntimeError(
            "Transcrição falhou "
            "na validação final."
        )

    fetched_at = datetime.now(
        ZoneInfo(
            "America/Sao_Paulo"
        )
    ).isoformat(
        timespec="seconds"
    )

    output = (
        f"VIDEO_ID: {video_id}\n"
        f"URL: {youtube_url}\n"
        f"TITLE: {title}\n"
        f"FETCHED_AT: {fetched_at}\n"
        f"SOURCE: {source}\n"
        f"TRANSCRIPT_LENGTH: {len(transcript)}\n"
        "\n"
        f"{transcript}\n"
    )

    temp_file = Path(
        "ultima_live_btg_nova.txt"
    )

    temp_file.write_text(
        output,
        encoding="utf-8",
    )

    saved = temp_file.read_text(
        encoding="utf-8",
        errors="ignore",
    )

    parts = saved.split(
        "\n\n",
        1,
    )

    if (
        len(parts) != 2
        or not validate_transcript(
            parts[1]
        )
    ):
        temp_file.unlink(
            missing_ok=True
        )

        raise RuntimeError(
            "Arquivo temporário "
            "não contém transcrição válida."
        )

    temp_file.replace(
        OUTPUT_FILE
    )

    print("")
    print("=" * 60)
    print("SUCESSO")
    print("=" * 60)
    print(
        f"VIDEO_ID: {video_id}"
    )
    print(
        f"TITLE: {title}"
    )
    print(
        f"URL: {youtube_url}"
    )
    print(
        f"SOURCE: {source}"
    )
    print(
        f"FETCHED_AT: {fetched_at}"
    )
    print(
        f"Tamanho: {len(transcript)} caracteres"
    )
    print(
        "Timestamps: "
        f"{count_timestamps(transcript)}"
    )
    print(
        f"Arquivo salvo: {OUTPUT_FILE}"
    )


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            viewport={
                "width": 1365,
                "height": 900,
            },
            user_agent=(
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
        )

        page = context.new_page()

        try:
            (
                video_id,
                youtube_url,
                title,
            ) = find_latest_completed_live(
                page
            )

            if (
                get_saved_video_id()
                == video_id
                and existing_file_is_valid_for(
                    video_id
                )
            ):
                print(
                    "A live concluída mais recente "
                    "já está salva e válida. "
                    "Nada a atualizar."
                )
                return

            (
                transcript,
                source,
            ) = get_transcript_with_retries(
                page,
                video_id,
                youtube_url,
            )

            if not transcript:
                raise RuntimeError(
                    "Não foi possível obter "
                    "uma transcrição válida no 2outube. "
                    "O ultima_live_btg.txt anterior "
                    "foi preservado."
                )

            save_transcript(
                video_id,
                youtube_url,
                title,
                transcript,
                source,
            )

        finally:
            browser.close()


if __name__ == "__main__":
    main()
