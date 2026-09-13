import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright


# =============================================================================
# CONFIGURAÇÃO POR VARIÁVEIS DE AMBIENTE
# =============================================================================

CHANNEL_NAME = os.getenv("CHANNEL_NAME", "Fabio Adriano").strip()
CHANNEL_URL = os.getenv(
    "CHANNEL_URL",
    "https://www.youtube.com/@fabioadriano/streams",
).strip()
CONTENT_KIND = os.getenv("CONTENT_KIND", "live").strip().lower()  # live | video
OUTPUT_FILE = Path(os.getenv("OUTPUT_FILE", "ultima_live.txt").strip())

# Estratégia de descoberta do conteúdo no canal.
# - latest: primeiro conteúdo válido e não agendado.
# - btg_recent_completed: para o BTG, procura especificamente uma live já
#   ENCERRADA e publicada/transmitida há 1 ou 2 dias. Isso evita selecionar
#   as lives futuras que aparecem no topo da aba /streams como "Programado".
DISCOVERY_MODE = os.getenv("DISCOVERY_MODE", "latest").strip().lower()
BTG_MIN_AGE_DAYS = int(os.getenv("BTG_MIN_AGE_DAYS", "1"))
BTG_MAX_AGE_DAYS = int(os.getenv("BTG_MAX_AGE_DAYS", "2"))

FORCE_RETRANSCRIBE = os.getenv("FORCE_RETRANSCRIBE", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# O ponto principal desta versão:
# depois de mandar o site gerar a transcrição, o script NÃO considera falha
# precocemente. Ele verifica a página a cada 5 segundos por até 150 segundos.
# Portanto, nenhum provedor de formulário é abandonado antes de 2 minutos.
TRANSCRIPT_WAIT_SECONDS = int(os.getenv("TRANSCRIPT_WAIT_SECONDS", "150"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))

# Evita o comportamento antigo de loop infinito. Se todos os provedores falharem,
# fazemos no máximo 2 ciclos completos e devolvemos erro para esse canal.
MAX_PROVIDER_CYCLES = int(os.getenv("MAX_PROVIDER_CYCLES", "2"))
WAIT_BETWEEN_CYCLES = int(os.getenv("WAIT_BETWEEN_CYCLES", "20"))

# Validação mínima da transcrição.
MIN_TRANSCRIPT_LENGTH = 2000
MIN_TRANSCRIPT_WORDS = 300
MIN_TIMESTAMP_COUNT = 3

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
    "502 bad gateway",
    "503 service unavailable",
)

PAGE_FOOTERS = (
    "Works on any YouTube video.",
    "Read another video",
    "ONE EMAIL, NO SPAM",
    "Get transcripts by email",
)


def normalize_channel_url(url: str) -> str:
    url = (url or "").strip()
    url = url.replace("https://m.youtube.com", "https://www.youtube.com")
    url = url.replace("http://m.youtube.com", "https://www.youtube.com")
    return url


CHANNEL_URL = normalize_channel_url(CHANNEL_URL)


# =============================================================================
# UTILIDADES
# =============================================================================

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


def canonical_video_url_from_href(href: str, video_id: str) -> str:
    href = href or ""
    if "/live/" in href:
        return f"https://www.youtube.com/live/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def count_timestamps(text: str) -> int:
    """Conta timestamp em linha isolada OU na mesma linha da fala.

    Aceita, por exemplo:
        05:19
        Fala galera...

    e também:
        05:19 Fala galera...
    """
    if not text:
        return 0

    return len(
        re.findall(
            r"(?m)^\s*\d{1,2}:\d{2}(?::\d{2})?(?:\s+|$)",
            text,
        )
    )


def normalize_whitespace(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def strip_page_chrome(text: str) -> str:
    """Remove o máximo possível de interface sem cortar a transcrição."""
    if not text:
        return ""

    text = normalize_whitespace(text)

    # O 2outube costuma colocar um marcador TRANSCRIPT antes do conteúdo.
    transcript_marker = re.search(r"(?mi)^\s*TRANSCRIPT\s*$", text)
    if transcript_marker:
        text = text[transcript_marker.start():]

    # Corta rodapés/promos conhecidos, quando presentes.
    end_positions = []
    for marker in PAGE_FOOTERS:
        pos = text.find(marker)
        if pos >= 0:
            end_positions.append(pos)

    if end_positions:
        text = text[: min(end_positions)]

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

    cleaned_lines = []
    for line in text.splitlines():
        if line.strip() in junk_lines:
            continue
        cleaned_lines.append(line)

    return normalize_whitespace("\n".join(cleaned_lines))


def validate_transcript(text: str) -> bool:
    """Aceita transcrições grandes e reais; rejeita página de erro/interface."""
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

    timestamp_count = count_timestamps(text)
    has_transcript_marker = bool(re.search(r"(?mi)^\s*TRANSCRIPT\s*$", text))

    # Caso principal: transcript com timestamps, inclusive quando o horário
    # aparece na mesma linha da fala.
    if timestamp_count >= MIN_TIMESTAMP_COUNT:
        return True

    # Fallback para sites que devolvem texto corrido, mas mantêm o marcador
    # explícito de transcrição e quantidade grande de palavras.
    if has_transcript_marker and len(words) >= 500:
        return True

    return False


def prepare_transcript(text: str):
    text = strip_page_chrome(text)
    return text if validate_transcript(text) else None


def get_saved_video_id():
    if not OUTPUT_FILE.exists():
        return None

    try:
        head = OUTPUT_FILE.read_text(encoding="utf-8", errors="ignore")[:1500]
    except Exception:
        return None

    match = re.search(r"(?m)^VIDEO_ID:\s*([A-Za-z0-9_-]{11})\s*$", head)
    return match.group(1) if match else None


def existing_file_is_valid_for(video_id: str) -> bool:
    if not OUTPUT_FILE.exists():
        return False

    try:
        text = OUTPUT_FILE.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False

    if f"VIDEO_ID: {video_id}" not in text[:1500]:
        return False

    if "\n\n" not in text:
        return False

    transcript_part = text.split("\n\n", 1)[1]
    return validate_transcript(transcript_part)


# =============================================================================
# DESCOBERTA DO ÚLTIMO VÍDEO/LIVE
# =============================================================================

# Marcadores de conteúdo futuro/agendado observados em português e inglês.
# "programado" é especialmente importante no BTG: o YouTube mostra várias
# lives futuras no topo da aba /streams.
FUTURE_MARKERS = (
    "programado",
    "programada",
    "agendado",
    "agendada",
    "estreia em",
    "estreia marcada",
    "em breve",
    "começa em",
    "comeca em",
    "scheduled",
    "upcoming",
    "premiere",
    "premieres",
)


def candidate_selectors():
    if CONTENT_KIND == "video":
        return (
            'ytd-rich-item-renderer a[href*="/watch?v="], '
            'ytd-rich-item-renderer a[href*="/live/"], '
            'ytd-grid-video-renderer a[href*="/watch?v="], '
            'a[href*="/watch?v="], a[href*="/live/"]',
        )

    return (
        'ytd-rich-item-renderer a[href*="/live/"], '
        'ytd-rich-item-renderer a[href*="/watch?v="], '
        'a[href*="/live/"], a[href*="/watch?v="]',
    )


def normalize_for_match(text: str) -> str:
    """Normaliza texto do card para comparações robustas em PT/EN."""
    import unicodedata

    text = (text or "").lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_card_for_link(link):
    """Tenta subir do <a> para o card completo do vídeo/live."""
    candidates = (
        "xpath=ancestor::ytd-rich-item-renderer[1]",
        "xpath=ancestor::ytd-grid-video-renderer[1]",
        "xpath=ancestor::ytd-video-renderer[1]",
        "xpath=ancestor::ytd-rich-grid-media[1]",
    )

    for selector in candidates:
        try:
            card = link.locator(selector)
            if card.count() > 0:
                return card
        except Exception:
            continue

    return None


def collect_card_text(link) -> str:
    """Reúne texto visível + atributos úteis do card.

    Em algumas versões do YouTube, "Programado para..." ou "Transmitido há..."
    aparece em aria-label/title e não necessariamente em text_content().
    """
    parts = []
    card = get_card_for_link(link)

    try:
        txt = (card.text_content(timeout=3000) if card is not None else "") or ""
        if txt:
            parts.append(txt)
    except Exception:
        pass

    # Atributos do próprio link.
    for attr in ("aria-label", "title"):
        try:
            value = link.get_attribute(attr)
            if value:
                parts.append(value)
        except Exception:
            pass

    # Atributos de elementos internos do card (título, badges, metadata).
    if card is not None:
        try:
            nodes = card.locator('[aria-label], [title]')
            for i in range(min(nodes.count(), 30)):
                node = nodes.nth(i)
                for attr in ("aria-label", "title"):
                    try:
                        value = node.get_attribute(attr)
                        if value:
                            parts.append(value)
                    except Exception:
                        pass
        except Exception:
            pass

    return normalize_whitespace("\n".join(parts))


def is_future_card(card_text: str) -> bool:
    normalized = normalize_for_match(card_text)
    return any(normalize_for_match(marker) in normalized for marker in FUTURE_MARKERS)


def extract_relative_age_days(card_text: str):
    """Extrai idade relativa do card quando o YouTube mostra 'há N dias'.

    Aceita exemplos como:
      - Transmitido há 1 dia
      - Transmitido há 2 dias
      - há 1 dia
      - 1 day ago / 2 days ago
      - streamed 1 day ago
      - ontem / yesterday
    """
    text = normalize_for_match(card_text)

    if re.search(r"\bontem\b|\byesterday\b", text):
        return 1

    patterns = (
        r"(?:transmitido|transmitida|publicado|publicada)?\s*ha\s+(\d+)\s+dias?\b",
        r"(?:streamed|published)?\s*(\d+)\s+days?\s+ago\b",
    )

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                return None

    return None


def looks_like_completed_live(card_text: str) -> bool:
    """Indícios de que o card representa transmissão já encerrada."""
    text = normalize_for_match(card_text)

    completed_markers = (
        "transmitido ha",
        "transmitida ha",
        "streamed",
        "foi transmitido",
        "foi transmitida",
        "ha 1 dia",
        "ha 2 dias",
        "1 day ago",
        "2 days ago",
        "ontem",
        "yesterday",
    )

    return any(marker in text for marker in completed_markers)


def discover_candidates(page, max_links=140):
    """Coleta candidatos únicos mantendo a ordem em que aparecem no canal."""
    candidates = []
    found_ids = set()

    for selector in candidate_selectors():
        links = page.locator(selector)
        count = links.count()

        for i in range(min(count, max_links)):
            try:
                link = links.nth(i)
                href = link.get_attribute("href")
                if not href or "/shorts/" in href:
                    continue

                video_id = extract_video_id(href)
                if not video_id or video_id in found_ids:
                    continue

                found_ids.add(video_id)
                card_text = collect_card_text(link)

                candidates.append(
                    {
                        "video_id": video_id,
                        "href": href,
                        "url": canonical_video_url_from_href(href, video_id),
                        "card_text": card_text,
                    }
                )
            except Exception as exc:
                print(f"Aviso ao analisar candidato {i}: {exc}")

    return candidates


def find_btg_recent_completed_live(page, candidates):
    """Seleciona uma live ENCERRADA do BTG com 1–2 dias de idade.

    Esta seleção é deliberadamente estrita. Se o YouTube não fornecer nenhum
    candidato dentro da janela de 1–2 dias, o script falha em vez de escolher
    acidentalmente uma live futura/agendada.
    """
    print(
        "Modo BTG: procurando live ENCERRADA entre "
        f"{BTG_MIN_AGE_DAYS} e {BTG_MAX_AGE_DAYS} dia(s) atrás..."
    )

    eligible = []

    for idx, item in enumerate(candidates, start=1):
        video_id = item["video_id"]
        card_text = item["card_text"]
        normalized = normalize_for_match(card_text)

        if is_future_card(card_text):
            print(f"BTG candidato {idx}: {video_id} -> IGNORADO (programado/futuro)")
            continue

        age_days = extract_relative_age_days(card_text)
        completed_hint = looks_like_completed_live(card_text)

        # Para auditoria no log, mostra apenas um resumo curto do card.
        preview = re.sub(r"\s+", " ", card_text).strip()[:180]
        print(
            f"BTG candidato {idx}: {video_id} | idade={age_days} | "
            f"encerrada={completed_hint} | {preview}"
        )

        if age_days is None:
            continue

        if not (BTG_MIN_AGE_DAYS <= age_days <= BTG_MAX_AGE_DAYS):
            continue

        # A aba /streams já restringe o universo a transmissões. Ainda assim,
        # exigimos algum indício textual de que a transmissão foi concluída.
        if not completed_hint:
            continue

        eligible.append((age_days, idx, item))

    if not eligible:
        raise RuntimeError(
            "BTG Trader: nenhuma live ENCERRADA encontrada na janela estrita "
            f"de {BTG_MIN_AGE_DAYS}–{BTG_MAX_AGE_DAYS} dias. "
            "As lives programadas foram deliberadamente ignoradas para evitar "
            "selecionar um vídeo futuro sem transcrição."
        )

    # Preferência: a mais recente dentro da janela. Em empate, mantém a ordem
    # em que o YouTube apresentou os cards.
    eligible.sort(key=lambda x: (x[0], x[1]))
    age_days, _, chosen = eligible[0]

    print("BTG live encerrada selecionada:")
    print(f"VIDEO_ID: {chosen['video_id']}")
    print(f"IDADE: {age_days} dia(s)")
    print(f"URL: {chosen['url']}")

    return chosen["video_id"], chosen["url"]


def find_latest_content(page):
    label = "live mais recente" if CONTENT_KIND == "live" else "vídeo mais recente"

    print(f"Abrindo canal: {CHANNEL_NAME}")
    print(f"URL do canal: {CHANNEL_URL}")
    print(f"DISCOVERY_MODE: {DISCOVERY_MODE}")

    page.goto(CHANNEL_URL, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(7000)

    print(f"Procurando {label}...")
    candidates = discover_candidates(page)

    if not candidates:
        raise RuntimeError(f"Nenhum conteúdo candidato encontrado em {CHANNEL_URL}.")

    if DISCOVERY_MODE == "btg_recent_completed":
        return find_btg_recent_completed_live(page, candidates)

    # Fluxo padrão para Fábio/Clube: pega o primeiro candidato que não esteja
    # claramente marcado como futuro/agendado.
    for item in candidates:
        video_id = item["video_id"]
        card_text = item["card_text"]

        if is_future_card(card_text):
            print(f"Ignorando conteúdo agendado/programado: {video_id}")
            continue

        print("Conteúdo candidato encontrado:")
        print(item["url"])
        return video_id, item["url"]

    raise RuntimeError(
        f"Não foi possível localizar {label} válido e não agendado em {CHANNEL_URL}."
    )


# =============================================================================
# LEITURA DA TRANSCRIÇÃO NA PÁGINA
# =============================================================================

def safe_element_text(element):
    """Lê texto sem depender de inner_text em nós que não sejam HTMLElement."""
    try:
        tag = (element.evaluate("el => (el.tagName || '').toLowerCase()") or "").lower()
    except Exception:
        tag = ""

    if tag in {"input", "textarea"}:
        try:
            return element.input_value(timeout=1500)
        except Exception:
            pass

    try:
        return element.text_content(timeout=2500) or ""
    except Exception:
        return ""


def extract_transcript_from_page(page):
    candidates = []

    # Seletores preferenciais. O body inteiro fica somente como último fallback.
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
            for i in range(min(locator.count(), 40)):
                try:
                    text = safe_element_text(locator.nth(i))
                    text = prepare_transcript(text)
                    if text:
                        candidates.append(text)
                except Exception:
                    # Não polui o log a cada nó inválido.
                    continue
        except Exception:
            continue

    try:
        body = page.locator("body").text_content(timeout=5000) or ""
        body = prepare_transcript(body)
        if body:
            candidates.append(body)
    except Exception:
        pass

    if not candidates:
        return None

    return max(candidates, key=len)


def poll_for_transcript(page, provider_name: str, max_wait_seconds: int):
    """Espera o provedor processar o vídeo antes de considerar falha.

    Verifica a cada POLL_INTERVAL_SECONDS e só abandona o provedor depois de
    max_wait_seconds. Com o default atual, são 150 s = 2 min 30 s.
    """
    elapsed = 0

    while elapsed < max_wait_seconds:
        page.wait_for_timeout(POLL_INTERVAL_SECONDS * 1000)
        elapsed += POLL_INTERVAL_SECONDS

        transcript = extract_transcript_from_page(page)
        if transcript:
            print(
                f"{provider_name}: transcrição válida após ~{elapsed}s "
                f"({len(transcript)} caracteres)."
            )
            return transcript

        print(f"{provider_name} ainda processando... {elapsed}s")

    print(
        f"{provider_name}: nenhuma transcrição válida após "
        f"{max_wait_seconds}s de espera."
    )
    return None


# =============================================================================
# PROVEDOR 1 — 2OUTUBE (PRINCIPAL)
# =============================================================================

def find_visible_input(page):
    selectors = (
        'input[type="url"]',
        'input[name*="url"]',
        'input[id*="url"]',
        'input[placeholder*="YouTube"]',
        'input[placeholder*="youtube"]',
        'input[placeholder*="Paste"]',
        'input[placeholder*="paste"]',
        'input[type="text"]',
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
    patterns = (
        re.compile(r"get\s+(free\s+)?transcript", re.I),
        re.compile(r"generate\s+transcript", re.I),
        re.compile(r"transcribe", re.I),
        re.compile(r"gerar\s+transcri", re.I),
        re.compile(r"transcri", re.I),
    )

    for pattern in patterns:
        try:
            buttons = page.get_by_role("button", name=pattern)
            for i in range(buttons.count()):
                candidate = buttons.nth(i)
                if candidate.is_visible():
                    candidate.click(timeout=15000)
                    return True
        except Exception:
            pass

    try:
        submits = page.locator('button[type="submit"], input[type="submit"]')
        for i in range(submits.count()):
            candidate = submits.nth(i)
            if candidate.is_visible():
                candidate.click(timeout=15000)
                return True
    except Exception:
        pass

    return False


def try_2outube_form(page, youtube_url):
    """Replica o caminho manual: abre 2outube, cola URL, manda gerar e espera."""
    print("")
    print("[2outube] Abrindo formulário...")

    try:
        page.goto("https://2outube.com/", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)

        input_box = find_visible_input(page)
        if input_box is None:
            print("[2outube] Campo de URL não encontrado.")
            return None

        print("[2outube] Colando URL do YouTube...")
        input_box.fill(youtube_url)
        page.wait_for_timeout(500)

        if not click_transcript_button(page):
            print("[2outube] Botão para gerar transcrição não encontrado.")
            return None

        print(
            f"[2outube] Solicitação enviada. Aguardando até "
            f"{TRANSCRIPT_WAIT_SECONDS}s pela geração..."
        )

        return poll_for_transcript(
            page,
            "2outube",
            TRANSCRIPT_WAIT_SECONDS,
        )

    except Exception as exc:
        print(f"[2outube] Falha: {exc}")
        return None


def try_2outube_direct(page, video_id):
    """Fallback para transcrição já cacheada/pronta no 2outube."""
    urls = (
        f"https://2outube.com/watch?v={video_id}",
        f"https://2outube.com/live/{video_id}",
    )

    for url in urls:
        try:
            print(f"[2outube-direct] Tentando: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=90000)

            transcript = poll_for_transcript(page, "2outube-direct", 30)
            if transcript:
                return transcript
        except Exception as exc:
            print(f"[2outube-direct] Falha: {exc}")

    return None


# =============================================================================
# PROVEDOR 2 — TUBETRANSCRIPT
# =============================================================================

def try_tubetranscript(page, youtube_url):
    print("")
    print("[TubeTranscript] Abrindo formulário...")

    try:
        page.goto(
            "https://tubetranscript.com/pt/",
            wait_until="domcontentloaded",
            timeout=90000,
        )
        page.wait_for_timeout(3000)

        input_box = find_visible_input(page)
        if input_box is None:
            print("[TubeTranscript] Campo de URL não encontrado.")
            return None

        input_box.fill(youtube_url)
        page.wait_for_timeout(500)

        if not click_transcript_button(page):
            print("[TubeTranscript] Botão para gerar transcrição não encontrado.")
            return None

        print(
            f"[TubeTranscript] Solicitação enviada. Aguardando até "
            f"{TRANSCRIPT_WAIT_SECONDS}s pela geração..."
        )

        return poll_for_transcript(
            page,
            "TubeTranscript",
            TRANSCRIPT_WAIT_SECONDS,
        )

    except Exception as exc:
        print(f"[TubeTranscript] Falha: {exc}")
        return None


# =============================================================================
# PROVEDOR 3 — YOUTUBE-TRANSCRIPT.AI (FALLBACK HTTP)
# =============================================================================

def try_youtube_transcript_ai(video_id):
    print("")
    print("[youtube-transcript.ai] Tentando fallback HTTP...")

    url = f"https://youtube-transcript.ai/transcript/{video_id}.txt"
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            "Accept": "text/plain,text/html,application/xhtml+xml",
        },
    )

    try:
        with urlopen(request, timeout=90) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            raw = response.read()

        if content_type and not any(
            item in content_type for item in ("text", "json", "html")
        ):
            print(
                "[youtube-transcript.ai] Content-Type inesperado: "
                f"{content_type}"
            )
            return None

        transcript = raw.decode("utf-8", errors="replace")
        transcript = prepare_transcript(transcript)

        if transcript:
            print(
                "[youtube-transcript.ai] Transcrição válida "
                f"({len(transcript)} caracteres)."
            )
            return transcript

    except HTTPError as exc:
        print(f"[youtube-transcript.ai] HTTP {exc.code}")
    except URLError as exc:
        print(f"[youtube-transcript.ai] Erro de rede: {exc}")
    except Exception as exc:
        print(f"[youtube-transcript.ai] Falha: {exc}")

    return None


# =============================================================================
# ORQUESTRAÇÃO DOS PROVEDORES — FINITA, SEM LOOP INFINITO
# =============================================================================

def try_all_providers(page, video_id, youtube_url):
    for cycle in range(1, MAX_PROVIDER_CYCLES + 1):
        print("")
        print("=" * 72)
        print(f"CICLO {cycle}/{MAX_PROVIDER_CYCLES} — {CHANNEL_NAME}")
        print("Prioridade: 2outube formulário -> 2outube direto -> TubeTranscript -> fallback")
        print("=" * 72)

        # 1) Caminho manual confirmado pelo usuário como funcional.
        transcript = try_2outube_form(page, youtube_url)
        if transcript:
            return transcript, "2outube-form"

        # 2) Se o serviço já tiver cacheado o vídeo, tenta URL direta.
        transcript = try_2outube_direct(page, video_id)
        if transcript:
            return transcript, "2outube-direct"

        # 3) Segundo site por formulário, também com >= 2 minutos de tolerância.
        transcript = try_tubetranscript(page, youtube_url)
        if transcript:
            return transcript, "TubeTranscript"

        # 4) Fallback HTTP final.
        transcript = try_youtube_transcript_ai(video_id)
        if transcript:
            return transcript, "youtube-transcript.ai"

        if cycle < MAX_PROVIDER_CYCLES:
            print(
                f"Nenhum provedor funcionou no ciclo {cycle}. "
                f"Aguardando {WAIT_BETWEEN_CYCLES}s antes do último retry..."
            )
            time.sleep(WAIT_BETWEEN_CYCLES)

    return None, None


# =============================================================================
# PERSISTÊNCIA ATÔMICA
# =============================================================================

def save_transcript(video_id, youtube_url, transcript, source):
    transcript = prepare_transcript(transcript)
    if not transcript:
        raise RuntimeError("Transcrição falhou na validação final.")

    fetched_at = datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(
        timespec="seconds"
    )

    output = (
        f"VIDEO_ID: {video_id}\n"
        f"URL: {youtube_url}\n"
        f"FETCHED_AT: {fetched_at}\n"
        f"SOURCE: {source}\n"
        f"TRANSCRIPT_LENGTH: {len(transcript)}\n\n"
        f"{transcript}\n"
    )

    temp_file = OUTPUT_FILE.with_name(
        f"{OUTPUT_FILE.stem}_novo{OUTPUT_FILE.suffix}"
    )
    temp_file.write_text(output, encoding="utf-8")

    saved = temp_file.read_text(encoding="utf-8", errors="ignore")
    transcript_part = saved.split("\n\n", 1)[1] if "\n\n" in saved else ""

    if not validate_transcript(transcript_part):
        temp_file.unlink(missing_ok=True)
        raise RuntimeError("Arquivo temporário não contém transcrição válida.")

    # Só substitui o arquivo anterior depois de toda a validação passar.
    temp_file.replace(OUTPUT_FILE)

    print("")
    print("=" * 72)
    print("TRANSCRIÇÃO SALVA COM SUCESSO")
    print("=" * 72)
    print(f"CHANNEL_NAME: {CHANNEL_NAME}")
    print(f"OUTPUT_FILE: {OUTPUT_FILE}")
    print(f"VIDEO_ID: {video_id}")
    print(f"URL: {youtube_url}")
    print(f"SOURCE: {source}")
    print(f"FETCHED_AT: {fetched_at}")
    print(f"TRANSCRIPT_LENGTH: {len(transcript)}")
    print(f"TIMESTAMPS: {count_timestamps(transcript)}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    if CONTENT_KIND not in {"live", "video"}:
        raise ValueError("CONTENT_KIND deve ser 'live' ou 'video'.")

    if TRANSCRIPT_WAIT_SECONDS < 120:
        raise ValueError(
            "TRANSCRIPT_WAIT_SECONDS deve ser >= 120 segundos nesta versão."
        )

    print("")
    print("#" * 72)
    print(f"CANAL: {CHANNEL_NAME}")
    print(f"TIPO: {CONTENT_KIND}")
    print(f"ARQUIVO: {OUTPUT_FILE}")
    print(f"ESPERA POR PROVEDOR: {TRANSCRIPT_WAIT_SECONDS}s")
    print(f"CICLOS MÁXIMOS: {MAX_PROVIDER_CYCLES}")
    print("#" * 72)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
        )
        page = context.new_page()

        try:
            video_id, youtube_url = find_latest_content(page)

            print("")
            print(f"VIDEO_ID encontrado: {video_id}")
            print(f"URL: {youtube_url}")

            saved_id = get_saved_video_id()
            if (
                saved_id == video_id
                and existing_file_is_valid_for(video_id)
                and not FORCE_RETRANSCRIBE
            ):
                print("")
                print("O conteúdo mais recente já possui transcrição válida.")
                print("Nenhuma nova chamada aos provedores será feita.")
                return

            if FORCE_RETRANSCRIBE:
                print("")
                print("FORCE_RETRANSCRIBE=1: retranscrição obrigatória.")

            transcript, source = try_all_providers(
                page,
                video_id,
                youtube_url,
            )

            if not transcript:
                raise RuntimeError(
                    "Nenhum provedor conseguiu gerar uma transcrição válida "
                    f"para {CHANNEL_NAME} após {MAX_PROVIDER_CYCLES} ciclo(s). "
                    f"O arquivo anterior '{OUTPUT_FILE}' foi preservado."
                )

            save_transcript(
                video_id,
                youtube_url,
                transcript,
                source,
            )

        finally:
            browser.close()


if __name__ == "__main__":
    main()
