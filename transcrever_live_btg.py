import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright


CHANNEL_URL = "https://www.youtube.com/@BTGTrader/streams"
OUTPUT_FILE = Path("ultima_live_btg.txt")

# Segurança contra workflows intermináveis
MAX_CYCLES = 4
WAIT_BETWEEN_ATTEMPTS = 20

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

    marker = re.search(
        r"(?mi)^\s*TRANSCRIPT\s*$",
        text,
    )

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
        line
        for line in text.splitlines()
        if line.strip() not in junk_lines
    ]

    return normalize_whitespace("\n".join(lines))


def validate_transcript(text: str) -> bool:
    """
    Aceita:
    - transcrições com timestamps; OU
    - transcrições longas sem timestamps.

    Isso evita rejeitar uma transcrição válida do TubeTranscript
    apenas porque o site não exibiu timestamps.
    """

    if not text:
        return False

    text = normalize_whitespace(text)

    if len(text) < MIN_TRANSCRIPT_LENGTH:
        return False

    words = re.findall(
        r"\b\w+\b",
        text,
        flags=re.UNICODE,
    )

    if len(words) < MIN_TRANSCRIPT_WORDS:
        return False

    lower = text.lower()

    if any(
        message in lower
        for message in BAD_MESSAGES
    ):
        return False

    # Caso ideal: transcript com timestamps
    if count_timestamps(text) >= MIN_TIMESTAMP_COUNT:
        return True

    # TubeTranscript ou outro provedor pode entregar
    # transcript longo sem timestamps.
    if len(words) >= 500:
        return True

    # Também aceita transcript explicitamente identificado.
    has_marker = bool(
        re.search(
            r"(?mi)^\s*TRANSCRIPT\s*$",
            text,
        )
    )

    return has_marker and len(words) >= 250


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
    print("Abrindo canal BTG Trader...", flush=True)

    page.goto(
        CHANNEL_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    page.wait_for_timeout(5000)

    print(
        "Procurando a live concluída mais recente na aba Ao vivo...",
        flush=True,
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

                video_id = extract_video_id(
                    href or ""
                )

                if (
                    not video_id
                    or video_id in seen
                ):
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
                        f"{video_id}",
                        flush=True,
                    )

                    continue

                youtube_url = (
                    f"https://www.youtube.com/live/{video_id}"
                )

                print(
                    f"VIDEO_ID encontrado: {video_id}",
                    flush=True,
                )
                print(
                    f"TITLE: {title}",
                    flush=True,
                )
                print(
                    f"URL: {youtube_url}",
                    flush=True,
                )

                return (
                    video_id,
                    youtube_url,
                    title,
                )

            except Exception as exc:
                print(
                    f"Aviso ao analisar card {i}: {exc}",
                    flush=True,
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
                            timeout=1500
                        )

                    except Exception:
                        text = element.inner_text(
                            timeout=2500
                        )

                    text = prepare_transcript(
                        text
                    )

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
            timeout=4000
        )

        body = prepare_transcript(
            body
        )

        if body:
            candidates.append(body)

    except Exception:
        pass

    return (
        max(
            candidates,
            key=len,
        )
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
                f"Tentando 2outube diretamente: {url}",
                flush=True,
            )

            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=45000,
            )

            page.wait_for_timeout(6000)

            transcript = (
                extract_transcript_from_page(page)
            )

            if transcript:
                print(
                    "Transcrição válida no 2outube "
                    f"({len(transcript)} caracteres).",
                    flush=True,
                )

                return transcript

        except Exception as exc:
            print(
                "Falha no acesso direto ao 2outube: "
                f"{exc}",
                flush=True,
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
        'input[placeholder*="URL"]',
        'input[type="text"]',
    )

    for selector in selectors:
        try:
            locator = page.locator(
                selector
            )

            for i in range(
                min(locator.count(), 20)
            ):
                candidate = locator.nth(i)

                try:
                    if candidate.is_visible():
                        return candidate

                except Exception:
                    continue

        except Exception:
            continue

    return None


def click_2outube_button(page):
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

        for i in range(
            min(buttons.count(), 20)
        ):
            candidate = buttons.nth(i)

            if candidate.is_visible():
                candidate.click(
                    timeout=10000
                )

                return True

    except Exception:
        pass

    try:
        buttons = page.locator(
            'button[type="submit"], '
            'input[type="submit"]'
        )

        for i in range(
            min(buttons.count(), 20)
        ):
            candidate = buttons.nth(i)

            if candidate.is_visible():
                candidate.click(
                    timeout=10000
                )

                return True

    except Exception:
        pass

    return False


def try_2outube_form(page, youtube_url):
    print(
        "Tentando formulário do 2outube...",
        flush=True,
    )

    try:
        page.goto(
            "https://2outube.com/",
            wait_until="domcontentloaded",
            timeout=45000,
        )

        page.wait_for_timeout(3000)

        input_box = find_visible_input(
            page
        )

        if input_box is None:
            print(
                "Campo de URL não encontrado no 2outube.",
                flush=True,
            )

            return None

        input_box.fill(
            youtube_url
        )

        page.wait_for_timeout(
            500
        )

        if not click_2outube_button(
            page
        ):
            print(
                "Botão de transcrição não encontrado no 2outube.",
                flush=True,
            )

            return None

        # Até 30 segundos para aparecer
        # uma transcrição válida.
        for wait_round in range(1, 7):
            page.wait_for_timeout(
                5000
            )

            transcript = (
                extract_transcript_from_page(page)
            )

            if transcript:
                print(
                    "Transcrição válida via formulário 2outube "
                    f"({len(transcript)} caracteres).",
                    flush=True,
                )

                return transcript

            print(
                "2outube ainda sem transcrição válida "
                f"({wait_round * 5}s).",
                flush=True,
            )

    except Exception as exc:
        print(
            "Falha no formulário do 2outube: "
            f"{exc}",
            flush=True,
        )

    return None


def click_tubetranscript_button(page):
    patterns = (
        re.compile(
            r"gerar\s+transcri",
            re.I,
        ),
        re.compile(
            r"generate\s+transcript",
            re.I,
        ),
        re.compile(
            r"transcri",
            re.I,
        ),
    )

    for pattern in patterns:
        try:
            buttons = page.get_by_role(
                "button",
                name=pattern,
            )

            for i in range(
                min(buttons.count(), 20)
            ):
                candidate = buttons.nth(i)

                if candidate.is_visible():
                    candidate.click(
                        timeout=10000
                    )

                    return True

        except Exception:
            continue

    try:
        submit = page.locator(
            'button[type="submit"], '
            'input[type="submit"]'
        )

        for i in range(
            min(submit.count(), 20)
        ):
            candidate = submit.nth(i)

            if candidate.is_visible():
                candidate.click(
                    timeout=10000
                )

                return True

    except Exception:
        pass

    return False


def try_tubetranscript(
    page,
    youtube_url,
):
    print(
        "Tentando TubeTranscript...",
        flush=True,
    )

    try:
        page.goto(
            "https://tubetranscript.com/pt/",
            wait_until="domcontentloaded",
            timeout=45000,
        )

        page.wait_for_timeout(
            3000
        )

        input_box = find_visible_input(
            page
        )

        if input_box is None:
            print(
                "Campo de URL não encontrado no TubeTranscript.",
                flush=True,
            )

            return None

        input_box.fill(
            youtube_url
        )

        page.wait_for_timeout(
            500
        )

        if not click_tubetranscript_button(
            page
        ):
            print(
                "Botão 'Gerar transcrição' "
                "não encontrado no TubeTranscript.",
                flush=True,
            )

            return None

        # Máximo de aproximadamente 40 segundos
        # esperando a geração.
        for wait_round in range(1, 9):
            page.wait_for_timeout(
                5000
            )

            transcript = (
                extract_transcript_from_page(page)
            )

            if transcript:
                print(
                    "Transcrição válida no TubeTranscript "
                    f"({len(transcript)} caracteres).",
                    flush=True,
                )

                return transcript

            print(
                "TubeTranscript ainda processando... "
                f"{wait_round * 5}s",
                flush=True,
            )

    except Exception as exc:
        print(
            f"Falha no TubeTranscript: {exc}",
            flush=True,
        )

    return None


def save_debug_page(
    page,
    cycle,
):
    try:
        filename = Path(
            f"debug_btg_transcript_cycle_{cycle}.html"
        )

        filename.write_text(
            page.content(),
            encoding="utf-8",
        )

        print(
            f"Página de diagnóstico salva: {filename}",
            flush=True,
        )

    except Exception as exc:
        print(
            "Não foi possível salvar diagnóstico: "
            f"{exc}",
            flush=True,
        )


def get_transcript_with_retries(
    page,
    video_id,
    youtube_url,
):
    """
    Faz no máximo quatro ciclos.

    Cada ciclo:
    1. 2outube direto
    2. formulário do 2outube
    3. TubeTranscript

    Depois de quatro ciclos, encerra com erro.
    """

    for cycle in range(
        1,
        MAX_CYCLES + 1,
    ):
        print("")
        print(
            "=" * 60,
            flush=True,
        )
        print(
            f"CICLO {cycle}/{MAX_CYCLES}: "
            "2outube -> TubeTranscript",
            flush=True,
        )
        print(
            "=" * 60,
            flush=True,
        )

        transcript = (
            try_2outube_direct(
                page,
                video_id,
            )
        )

        if transcript:
            return (
                transcript,
                "2outube-direct",
            )

        transcript = (
            try_2outube_form(
                page,
                youtube_url,
            )
        )

        if transcript:
            return (
                transcript,
                "2outube-form",
            )

        transcript = (
            try_tubetranscript(
                page,
                youtube_url,
            )
        )

        if transcript:
            return (
                transcript,
                "TubeTranscript",
            )

        save_debug_page(
            page,
            cycle,
        )

        if cycle < MAX_CYCLES:
            print(
                "Nenhum provedor retornou "
                "transcrição válida neste ciclo.",
                flush=True,
            )
            print(
                f"Novo ciclo em {WAIT_BETWEEN_ATTEMPTS}s...",
                flush=True,
            )

            time.sleep(
                WAIT_BETWEEN_ATTEMPTS
            )

    raise RuntimeError(
        f"Falha após {MAX_CYCLES} ciclos completos. "
        "2outube e TubeTranscript não retornaram "
        "uma transcrição váli