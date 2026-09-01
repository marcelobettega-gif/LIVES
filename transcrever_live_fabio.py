import re
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright


CHANNEL_URL = "https://www.youtube.com/@fabioadriano/streams"
OUTPUT_FILE = Path("ultima_live.txt")

WAIT_BETWEEN_ROUNDS = 45
MIN_TRANSCRIPT_LENGTH = 2000
MIN_TRANSCRIPT_WORDS = 300
MIN_TIMESTAMP_COUNT = 5

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


# -----------------------------------------------------------------------------
# Utilidades
# -----------------------------------------------------------------------------

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


def count_timestamps(text: str) -> int:
    return len(
        re.findall(
            r"(?m)(?:^|\n)\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:\n|$)",
            text or "",
        )
    )


def normalize_whitespace(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def strip_page_chrome(text: str) -> str:
    """Remove cabeçalho/rodapé do site sem cortar o conteúdo da transcrição."""
    if not text:
        return ""

    text = normalize_whitespace(text)

    # No 2outube, o marcador TRANSCRIPT aparece imediatamente antes do título,
    # metadados e falas. Tudo anterior é navegação/interface.
    transcript_marker = re.search(r"(?mi)^\s*TRANSCRIPT\s*$", text)
    if transcript_marker:
        text = text[transcript_marker.start():]

    # Remove rodapés/promos que aparecem depois da fala final.
    end_positions = []
    for marker in PAGE_FOOTERS:
        pos = text.find(marker)
        if pos >= 0:
            end_positions.append(pos)

    if end_positions:
        text = text[: min(end_positions)]

    # Linhas de interface conhecidas que podem sobrar dentro do bloco.
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
    """Validação positiva e negativa para evitar salvar HTML/texto de interface."""
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

    # As fontes usadas normalmente trazem timestamps. Como alguns fallbacks
    # podem retornar texto corrido, também aceitamos bloco grande com marcador
    # explícito de transcrição.
    if timestamp_count >= MIN_TIMESTAMP_COUNT:
        return True

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
        head = OUTPUT_FILE.read_text(encoding="utf-8", errors="ignore")[:1000]
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

    if f"VIDEO_ID: {video_id}" not in text[:1000]:
        return False

    # Remove apenas o cabeçalho que o próprio script adiciona.
    transcript_part = re.sub(
        r"\A(?:VIDEO_ID:.*\nURL:.*\n(?:FETCHED_AT:.*\n)?(?:SOURCE:.*\n)?(?:TRANSCRIPT_LENGTH:.*\n)?\n?)",
        "",
        text,
        count=1,
        flags=re.MULTILINE,
    )

    return validate_transcript(transcript_part)


# -----------------------------------------------------------------------------
# Descoberta da live
# -----------------------------------------------------------------------------

def find_latest_live(page):
    print("Abrindo canal do Fabio Adriano...")

    page.goto(
        CHANNEL_URL,
        wait_until="domcontentloaded",
        timeout=90000,
    )
    page.wait_for_timeout(7000)

    print("Procurando a live mais recente na aba Ao vivo...")

    # Primeiro tenta links dentro dos cards da grade. Se o YouTube mudar o DOM,
    # cai para o seletor amplo usado na versão anterior.
    selectors = (
        'ytd-rich-item-renderer a[href*="/live/"], '
        'ytd-rich-item-renderer a[href*="/watch?v="]',
        'a[href*="/live/"], a[href*="/watch?v="]',
    )

    found_ids = set()

    for selector in selectors:
        links = page.locator(selector)
        count = links.count()

        for i in range(min(count, 80)):
            try:
                link = links.nth(i)
                href = link.get_attribute("href")
                if not href:
                    continue

                video_id = extract_video_id(href)
                if not video_id or video_id in found_ids:
                    continue

                found_ids.add(video_id)

                # Evita cards claramente agendados/futuros. Se não houver texto
                # no ancestral, não bloqueia o candidato.
                card_text = ""
                try:
                    card = link.locator("xpath=ancestor::ytd-rich-item-renderer[1]")
                    if card.count() > 0:
                        card_text = card.inner_text(timeout=2000).lower()
                except Exception:
                    pass

                if any(
                    marker in card_text
                    for marker in ("agendado", "scheduled", "estreia em", "premieres")
                ):
                    print(f"Ignorando vídeo agendado: {video_id}")
                    continue

                youtube_url = f"https://www.youtube.com/live/{video_id}"
                print("Primeiro candidato válido encontrado:")
                print(youtube_url)
                return video_id, youtube_url

            except Exception as exc:
                print(f"Aviso ao analisar link {i}: {exc}")

    raise RuntimeError("Não foi possível localizar nenhum vídeo na aba Ao vivo.")


# -----------------------------------------------------------------------------
# Extração a partir de páginas
# -----------------------------------------------------------------------------

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

            for i in range(min(locator.count(), 30)):
                try:
                    element = locator.nth(i)

                    try:
                        text = element.input_value(timeout=2000)
                    except Exception:
                        text = element.inner_text(timeout=3000)

                    text = prepare_transcript(text)
                    if text:
                        candidates.append(text)

                except Exception as exc:
                    print(f"Aviso no seletor {selector}[{i}]: {exc}")

        except Exception as exc:
            print(f"Aviso ao consultar seletor {selector}: {exc}")

    # Fallback: corpo inteiro, mas sempre passando por limpeza + validação forte.
    try:
        body = page.locator("body").inner_text(timeout=5000)
        body = prepare_transcript(body)
        if body:
            candidates.append(body)
    except Exception as exc:
        print(f"Aviso ao ler body: {exc}")

    if not candidates:
        return None

    # Prefere o maior bloco entre candidatos já validados.
    return max(candidates, key=len)


# -----------------------------------------------------------------------------
# Provedores
# -----------------------------------------------------------------------------

def try_2outube_direct(page, video_id):
    urls = (
        f"https://2outube.com/watch?v={video_id}",
        f"https://2outube.com/live/{video_id}",
    )

    for url in urls:
        try:
            print(f"Tentando 2outube diretamente: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(8000)

            transcript = extract_transcript_from_page(page)
            if transcript:
                print(f"Transcrição válida no 2outube direto ({len(transcript)} caracteres).")
                return transcript

        except Exception as exc:
            print(f"Falha no acesso direto ao 2outube: {exc}")

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
        r"get\s+(free\s+)?transcript|generate\s+transcript|transcribe|generate|submit",
        re.I,
    )

    try:
        buttons = page.get_by_role("button", name=pattern)
        for i in range(buttons.count()):
            candidate = buttons.nth(i)
            if candidate.is_visible():
                candidate.click(timeout=15000)
                return True
    except Exception:
        pass

    # Último fallback: somente submit explícito; não clica em qualquer botão.
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
    print("Abrindo página inicial do 2outube...")

    try:
        page.goto("https://2outube.com/", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(4000)

        input_box = find_visible_input(page)
        if input_box is None:
            print("Campo de URL não encontrado no 2outube.")
            return None

        print("Colando URL completa...")
        input_box.fill(youtube_url)
        page.wait_for_timeout(700)

        if not click_transcript_button(page):
            print("Botão de transcrição não encontrado no 2outube.")
            return None

        print("Solicitando transcrição...")
        page.wait_for_timeout(12000)

        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        transcript = extract_transcript_from_page(page)
        if transcript:
            print(f"Transcrição válida no formulário do 2outube ({len(transcript)} caracteres).")
            return transcript

    except Exception as exc:
        print(f"Falha no formulário do 2outube: {exc}")

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
            # O campo pode não ser type=url; procura pelo placeholder/documentação atual.
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
        patterns = (
            re.compile(r"gerar\s+transcri", re.I),
            re.compile(r"generate\s+transcript", re.I),
            re.compile(r"transcri", re.I),
        )
        for pattern in patterns:
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

        # Lives longas podem levar mais tempo. Faz polling sem recarregar a página.
        for wait_round in range(1, 13):
            page.wait_for_timeout(10000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass

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


def try_all_providers_with_retries(page, video_id, youtube_url):
    """Alterna 2outube -> TubeTranscript até obter uma transcrição válida.

    O limite prático é o timeout do job do GitHub Actions. O arquivo anterior nunca é
    apagado antes de uma nova transcrição passar pela validação.
    """
    cycle = 0
    while True:
        cycle += 1
        print("")
        print("=" * 60)
        print(f"CICLO DE TRANSCRIÇÃO {cycle}: 2outube -> TubeTranscript")
        print("=" * 60)

        # 2outube: primeiro acesso direto, depois formulário.
        transcript = try_2outube_direct(page, video_id)
        if transcript:
            return transcript, "2outube-direct"

        transcript = try_2outube_form(page, youtube_url)
        if transcript:
            return transcript, "2outube-form"

        # Se o 2outube falhar, tenta o segundo provedor solicitado.
        transcript = try_tubetranscript(page, youtube_url)
        if transcript:
            return transcript, "TubeTranscript"

        # Guarda diagnóstico da última página, mas não altera o .txt válido anterior.
        try:
            Path(f"debug_transcript_cycle_{cycle}.html").write_text(
                page.content(), encoding="utf-8"
            )
        except Exception:
            pass

        print(
            "2outube e TubeTranscript não retornaram transcrição válida. "
            f"Novo ciclo em {WAIT_BETWEEN_ROUNDS} segundos..."
        )
        time.sleep(WAIT_BETWEEN_ROUNDS)

def save_transcript(video_id, youtube_url, transcript, source):
    transcript = prepare_transcript(transcript)
    if not transcript:
        raise RuntimeError("Transcrição falhou na validação final.")

    fetched_at = datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds")

    output = (
        f"VIDEO_ID: {video_id}\n"
        f"URL: {youtube_url}\n"
        f"FETCHED_AT: {fetched_at}\n"
        f"SOURCE: {source}\n"
        f"TRANSCRIPT_LENGTH: {len(transcript)}\n\n"
        f"{transcript}\n"
    )

    temp_file = Path("ultima_live_nova.txt")
    temp_file.write_text(output, encoding="utf-8")

    # Valida novamente o arquivo efetivamente gravado antes da troca atômica.
    saved = temp_file.read_text(encoding="utf-8", errors="ignore")
    transcript_part = saved.split("\n\n", 1)[1] if "\n\n" in saved else ""

    if not validate_transcript(transcript_part):
        temp_file.unlink(missing_ok=True)
        raise RuntimeError("Arquivo temporário não contém transcrição válida.")

    temp_file.replace(OUTPUT_FILE)

    print("")
    print("=" * 60)
    print("SUCESSO")
    print("=" * 60)
    print(f"VIDEO_ID: {video_id}")
    print(f"URL: {youtube_url}")
    print(f"SOURCE: {source}")
    print(f"FETCHED_AT: {fetched_at}")
    print(f"Tamanho da transcrição: {len(transcript)} caracteres")
    print(f"Timestamps detectados: {count_timestamps(transcript)}")
    print(f"Arquivo salvo: {OUTPUT_FILE}")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main():
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
            video_id, youtube_url = find_latest_live(page)

            print("")
            print(f"VIDEO_ID encontrado: {video_id}")
            print(f"URL completa: {youtube_url}")

            saved_id = get_saved_video_id()
            if saved_id == video_id and existing_file_is_valid_for(video_id):
                print("")
                print("A última live já está salva e a transcrição anterior é válida.")
                print("Nenhuma nova consulta aos provedores é necessária.")
                return

            transcript, source = try_all_providers_with_retries(
                page,
                video_id,
                youtube_url,
            )

            if not transcript:
                raise RuntimeError(
                    "Nenhuma das rotas conseguiu obter uma transcrição válida da live. "
                    "O ultima_live.txt anterior foi preservado."
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
