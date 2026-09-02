import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright


# ============================================================
# CONFIGURAÇÃO
# ============================================================

CHANNEL_URL = "https://www.youtube.com/@BTGTrader/streams"
OUTPUT_FILE = Path("ultima_live_btg.txt")

# Máximo de ciclos:
# cada ciclo tenta 2outube e depois TubeTranscript.
MAX_CYCLES = 4

# Espera entre ciclos completos.
WAIT_BETWEEN_ATTEMPTS = 20

# Critérios mínimos de validação.
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
    "access denied",
)


PAGE_FOOTERS = (
    "Works on any YouTube video.",
    "Read another video",
    "ONE EMAIL, NO SPAM",
    "Get transcripts by email",
)


# ============================================================
# FUNÇÕES BÁSICAS
# ============================================================

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

    text = re.sub(
        r"[ \t]+\n",
        "\n",
        text,
    )

    text = re.sub(
        r"\n{4,}",
        "\n\n\n",
        text,
    )

    return text.strip()


def count_timestamps(text: str) -> int:
    return len(
        re.findall(
            r"(?m)(?:^|\n)\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:\n|$)",
            text or "",
        )
    )


# ============================================================
# LIMPEZA DA TRANSCRIÇÃO
# ============================================================

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
        position = text.find(footer)

        if position >= 0:
            endings.append(position)

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

    lines = []

    for line in text.splitlines():
        if line.strip() not in junk_lines:
            lines.append(line)

    return normalize_whitespace(
        "\n".join(lines)
    )


# ============================================================
# VALIDAÇÃO
# ============================================================

def validate_transcript(text: str) -> bool:
    """
    Considera válida uma transcrição que:

    1. tenha tamanho e número de palavras mínimos;
    2. não contenha mensagens conhecidas de erro;
    3. tenha timestamps suficientes;

    OU

    4. seja suficientemente longa mesmo sem timestamps.

    Isso é importante porque o TubeTranscript pode gerar
    texto válido sem timestamps.
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

    for message in BAD_MESSAGES:
        if message in lower:
            return False

    # Cenário ideal: timestamps presentes.
    if count_timestamps(text) >= MIN_TIMESTAMP_COUNT:
        return True

    # Transcrição longa sem timestamps.
    if len(words) >= 500:
        return True

    # Algumas páginas identificam explicitamente TRANSCRIPT.
    has_marker = bool(
        re.search(
            r"(?mi)^\s*TRANSCRIPT\s*$",
            text,
        )
    )

    if has_marker and len(words) >= 250:
        return True

    return False


def prepare_transcript(text: str):
    text = strip_page_chrome(text)

    if validate_transcript(text):
        return text

    return None


# ============================================================
# VERIFICAÇÃO DO ARQUIVO JÁ EXISTENTE
# ============================================================

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

    if match:
        return match.group(1)

    return None


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

    parts = text.split(
        "\n\n",
        1,
    )

    if len(parts) != 2:
        return False

    return validate_transcript(
        parts[1]
    )


# ============================================================
# LOCALIZAR A LIVE MAIS RECENTE DO BTG
# ============================================================

def find_latest_completed_live(page):
    print(
        "Abrindo canal BTG Trader...",
        flush=True,
    )

    page.goto(
        CHANNEL_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    page.wait_for_timeout(
        5000
    )

    print(
        "Procurando a live concluída mais recente na aba Ao vivo...",
        flush=True,
    )

    selectors = (
        (
            'ytd-rich-item-renderer '
            'a[href*="/live/"], '
            'ytd-rich-item-renderer '
            'a[href*="/watch?v="]'
        ),
        (
            'a[href*="/live/"], '
            'a[href*="/watch?v="]'
        ),
    )

    seen = set()

    for selector in selectors:
        links = page.locator(
            selector
        )

        total = min(
            links.count(),
            100,
        )

        for i in range(total):
            try:
                link = links.nth(i)

                href = link.get_attribute(
                    "href"
                )

                video_id = extract_video_id(
                    href or ""
                )

                if not video_id:
                    continue

                if video_id in seen:
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
                                line.strip()
                                for line in card_text.splitlines()
                                if line.strip()
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

                is_future_or_live = any(
                    marker in lower
                    for marker in future_or_live_markers
                )

                if is_future_or_live:
                    print(
                        "Ignorando live futura/em andamento: "
                        f"{video_id}",
                        flush=True,
                    )

                    continue

                youtube_url = (
                    "https://www.youtube.com/live/"
                    f"{video_id}"
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


# ============================================================
# EXTRAIR TEXTO DE UMA PÁGINA
# ============================================================

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
            locator = page.locator(
                selector
            )

            total = min(
                locator.count(),
                30,
            )

            for i in range(total):
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
                        candidates.append(
                            text
                        )

                except Exception:
                    continue

        except Exception:
            continue

    # Fallback: corpo inteiro da página.
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
            candidates.append(
                body
            )

    except Exception:
        pass

    if not candidates:
        return None

    return max(
        candidates,
        key=len,
    )


# ============================================================
# 2OUTUBE — ACESSO DIRETO
# ============================================================

def try_2outube_direct(
    page,
    video_id,
):
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

            page.wait_for_timeout(
                6000
            )

            transcript = extract_transcript_from_page(
                page
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


# ============================================================
# LOCALIZAR CAMPO DE URL
# ============================================================

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

            total = min(
                locator.count(),
                20,
            )

            for i in range(total):
                candidate = locator.nth(i)

                try:
                    if candidate.is_visible():
                        return candidate

                except Exception:
                    continue

        except Exception:
            continue

    return None


# ============================================================
# 2OUTUBE — FORMULÁRIO
# ============================================================

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

        total = min(
            buttons.count(),
            20,
        )

        for i in range(total):
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

        total = min(
            buttons.count(),
            20,
        )

        for i in range(total):
            candidate = buttons.nth(i)

            if candidate.is_visible():
                candidate.click(
                    timeout=10000
                )

                return True

    except Exception:
        pass

    return False


def try_2outube_form(
    page,
    youtube_url,
):
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

        page.wait_for_timeout(
            3000
        )

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

        # Espera até aproximadamente 30 segundos.
        for wait_round in range(1, 7):
            page.wait_for_timeout(
                5000
            )

            transcript = extract_transcript_from_page(
                page
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


# ============================================================
# TUBETRANSCRIPT
# ============================================================

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

            total = min(
                buttons.count(),
                20,
            )

            for i in range(total):
                candidate = buttons.nth(i)

                if candidate.is_visible():
                    candidate.click(
                        timeout=10000
                    )

                    return True

        except Exception:
            continue

    # Fallback para botão submit.
    try:
        submit = page.locator(
            'button[type="submit"], '
            'input[type="submit"]'
        )

        total = min(
            submit.count(),
            20,
        )

        for i in range(total):
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

        # Espera no máximo aproximadamente 40 segundos.
        for wait_round in range(1, 9):
            page.wait_for_timeout(
                5000
            )

            transcript = extract_transcript_from_page(
                page
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


# ============================================================
# DEBUG
# ============================================================

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


# ============================================================
# CICLOS DE TENTATIVA
# ============================================================

def get_transcript_with_retries(
    page,
    video_id,
    youtube_url,
):
    """
    Executa no máximo MAX_CYCLES ciclos.

    Cada ciclo tenta:

    1. 2outube por URL direta
    2. formulário do 2outube
    3. TubeTranscript

    Se nenhum funcionar após quatro ciclos,
    encerra com erro em vez de ficar em loop infinito.
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

        # ----------------------------------------------------
        # 1. 2OUTUBE DIRETO
        # ----------------------------------------------------

        transcript = try_2outube_direct(
            page,
            video_id,
        )

        if transcript:
            return (
                transcript,
                "2outube-direct",
            )

        # ----------------------------------------------------
        # 2. 2OUTUBE FORMULÁRIO
        # ----------------------------------------------------

        transcript = try_2outube_form(
            page,
            youtube_url,
        )

        if transcript:
            return (
                transcript,
                "2outube-form",
            )

        # ----------------------------------------------------
        # 3. TUBETRANSCRIPT
        # ----------------------------------------------------

        transcript = try_tubetranscript(
            page,
            youtube_url,
        )

        if transcript:
            return (
                transcript,
                "TubeTranscript",
            )

        # Salva diagnóstico do último site acessado.
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

    # IMPORTANTE:
    # aqui o programa obrigatoriamente encerra.
    # Não existe while True.
    raise RuntimeError(
        f"Falha após {MAX_CYCLES} ciclos completos. "
        "2outube e TubeTranscript não retornaram "
        "uma transcrição válida. "
        "O ultima_live_btg.txt anterior foi preservado."
    )


# ============================================================
# SALVAR RESULTADO
# ============================================================

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
            "Transcrição falhou na validação final."
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

    # Confere novamente o arquivo gravado
    # antes de substituir o TXT anterior.
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
            "Arquivo temporário não contém "
            "uma transcrição válida."
        )

    # Só substitui o arquivo anterior
    # depois de todas as validações.
    temp_file.replace(
        OUTPUT_FILE
    )

    print("")
    print(
        "=" * 60,
        flush=True,
    )

    print(
        "SUCESSO",
        flush=True,
    )

    print(
        "=" * 60,
        flush=True,
    )

    print(
        f"VIDEO_ID: {video_id}",
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

    print(
        f"SOURCE: {source}",
        flush=True,
    )

    print(
        f"FETCHED_AT: {fetched_at}",
        flush=True,
    )

    print(
        f"Tamanho: {len(transcript)} caracteres",
        flush=True,
    )

    print(
        f"Timestamps: {count_timestamps(transcript)}",
        flush=True,
    )

    print(
        f"Arquivo salvo: {OUTPUT_FILE}",
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

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
            # ------------------------------------------------
            # Localiza a última live concluída.
            # ------------------------------------------------

            (
                video_id,
                youtube_url,
                title,
            ) = find_latest_completed_live(
                page
            )

            # ------------------------------------------------
            # Se a live atual já estiver salva e válida,
            # não desperdiça tempo retranscrevendo.
            # ------------------------------------------------

            saved_video_id = get_saved_video_id()

            if (
                saved_video_id == video_id
                and existing_file_is_valid_for(
                    video_id
                )
            ):
                print(
                    "A live concluída mais recente "
                    "já está salva e válida. "
                    "Nada a atualizar.",
                    flush=True,
                )

                return

            # ------------------------------------------------
            # Tenta obter a transcrição.
            # ------------------------------------------------

            (
                transcript,
                source,
            ) = get_transcript_with_retries(
                page,
                video_id,
                youtube_url,
            )

            # ------------------------------------------------
            # Salva somente após validação.
            # ------------------------------------------------

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