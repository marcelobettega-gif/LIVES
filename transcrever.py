import re
from pathlib import Path
from playwright.sync_api import sync_playwright

CHANNEL = "https://www.youtube.com/@fabioadriano/streams"
TRANSCRIBER = "https://2outube.com/"

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
        )

        page = context.new_page()

        # =================================================
        # 1. ABRIR A ABA AO VIVO DO CANAL
        # =================================================

        print("Abrindo canal do Fabio Adriano...")

        page.goto(
            CHANNEL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(6000)

        # =================================================
        # 2. PEGAR O PRIMEIRO VÍDEO VISÍVEL DA ABA AO VIVO
        # =================================================

        print("Procurando o primeiro vídeo da aba Ao vivo...")

        links = page.locator(
            'a[href*="/live/"], a[href*="/watch?v="]'
        )

        first_video_url = None

        for i in range(links.count()):
            try:
                href = links.nth(i).get_attribute("href")

                if not href:
                    continue

                match = re.search(
                    r'(?:/live/|watch\?v=)([A-Za-z0-9_-]{11})',
                    href
                )

                if not match:
                    continue

                video_id = match.group(1)

                first_video_url = (
                    f"https://www.youtube.com/live/{video_id}"
                )

                break

            except Exception:
                continue

        if not first_video_url:
            raise RuntimeError(
                "Não foi possível identificar o primeiro vídeo da aba Ao vivo."
            )

        video_id = re.search(
            r'/live/([A-Za-z0-9_-]{11})',
            first_video_url
        ).group(1)

        print("Primeiro vídeo encontrado:")
        print(first_video_url)

        # =================================================
        # 3. ABRIR O 2OUTUBE
        # =================================================

        print("Abrindo 2outube...")

        page.goto(
            TRANSCRIBER,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(4000)

        # =================================================
        # 4. LOCALIZAR CAMPO DE URL
        # =================================================

        print("Procurando campo de URL...")

        input_box = None

        possible_inputs = [
            'input[type="url"]',
            'input[placeholder*="YouTube"]',
            'input[placeholder*="youtube"]',
            'input[placeholder*="Paste"]',
            'input[placeholder*="paste"]',
            'input[name*="url"]',
            'input[id*="url"]',
        ]

        for selector in possible_inputs:
            locator = page.locator(selector)

            if locator.count() > 0:
                input_box = locator.first
                break

        if input_box is None:
            raise RuntimeError(
                "Campo para colar a URL não encontrado no 2outube."
            )

        # =================================================
        # 5. COLAR A URL COMPLETA
        # =================================================

        print("Colando URL completa...")

        input_box.fill(first_video_url)

        page.wait_for_timeout(1000)

        # =================================================
        # 6. CLICAR NO BOTÃO DE TRANSCRIÇÃO
        # =================================================

        print("Procurando botão de transcrição...")

        button = None

        possible_buttons = [
            "Get Transcript",
            "Get Free Transcript",
            "Transcript",
            "Transcribe",
            "Generate",
        ]

        for text in possible_buttons:
            locator = page.get_by_text(
                text,
                exact=False
            )

            if locator.count() > 0:
                button = locator.first
                break

        if button is None:
            submit = page.locator(
                'button[type="submit"], input[type="submit"]'
            )

            if submit.count() > 0:
                button = submit.first

        if button is None:
            raise RuntimeError(
                "Botão para gerar a transcrição não encontrado."
            )

        print("Solicitando transcrição...")

        button.click()

        # =================================================
        # 7. ESPERAR O RESULTADO
        # =================================================

        page.wait_for_timeout(10000)

        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=30000
            )
        except Exception:
            pass

        print("Página de resultado carregada.")

        # =================================================
        # 8. PROCURAR A TRANSCRIÇÃO
        # =================================================

        candidates = []

        selectors = [
            "textarea",
            "pre",
            '[class*="transcript"]',
            '[id*="transcript"]',
            '[class*="caption"]',
            '[id*="caption"]',
            "article",
            "main",
        ]

        for selector in selectors:
            locator = page.locator(selector)

            for i in range(locator.count()):
                try:
                    element = locator.nth(i)

                    text = ""

                    try:
                        text = element.input_value().strip()
                    except Exception:
                        text = element.inner_text().strip()

                    if len(text) > 1000:
                        candidates.append(text)

                except Exception:
                    continue

        # =================================================
        # 9. FALLBACK: TEXTO DA PÁGINA INTEIRA
        # =================================================

        body_text = page.locator("body").inner_text()

        if len(body_text) > 1000:
            candidates.append(body_text)

        if not candidates:
            # salva HTML para diagnóstico
            Path("debug_2outube.html").write_text(
                page.content(),
                encoding="utf-8"
            )

            raise RuntimeError(
                "Não foi possível encontrar uma transcrição no 2outube."
            )

        transcript = max(
            candidates,
            key=len
        )

        # =================================================
        # 10. VALIDAÇÃO
        # =================================================

        if len(transcript) < 1000:
            Path("debug_2outube.html").write_text(
                page.content(),
                encoding="utf-8"
            )

            raise RuntimeError(
                "A transcrição encontrada é muito curta."
            )

        # =================================================
        # 11. SALVAR TXT
        # =================================================

        output = (
            f"VIDEO_ID: {video_id}\n"
            f"URL: {first_video_url}\n\n"
            f"{transcript}"
        )

        Path("ultima_live.txt").write_text(
            output,
            encoding="utf-8"
        )

        print("")
        print("SUCESSO.")
        print("Transcrição salva em ultima_live.txt")
        print(
            f"Tamanho: {len(transcript)} caracteres"
        )

        browser.close()


if __name__ == "__main__":
    main()
