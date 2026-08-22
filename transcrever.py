import re
from pathlib import Path
from playwright.sync_api import sync_playwright

CHANNEL = "https://www.youtube.com/@fabioadriano/streams"
TRANSCRIBER = "https://youtubetotranscript.com/"

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

        # -------------------------------------------------
        # 1. ENCONTRAR A LIVE MAIS RECENTE DO FÁBIO ADRIANO
        # -------------------------------------------------

        print("Abrindo canal do Fabio Adriano...")
        page.goto(CHANNEL, wait_until="domcontentloaded", timeout=60000)

        page.wait_for_timeout(5000)

        html = page.content()

        ids = re.findall(
            r'"videoId":"([A-Za-z0-9_-]{11})"',
            html
        )

        videos = list(dict.fromkeys(ids))

        if not videos:
            raise RuntimeError(
                "Nenhum vídeo encontrado na aba Ao vivo."
            )

        video_id = videos[0]

        youtube_url = (
            f"https://www.youtube.com/live/{video_id}"
        )

        print("Vídeo encontrado:")
        print(youtube_url)

        # -------------------------------------------------
        # 2. ABRIR O YOUTUBE TO TRANSCRIPT
        # -------------------------------------------------

        print("Abrindo YouTubeToTranscript...")

        page.goto(
            TRANSCRIBER,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(3000)

        # -------------------------------------------------
        # 3. LOCALIZAR O CAMPO DE URL
        # -------------------------------------------------

        input_box = page.locator(
            'input[placeholder*="YouTube URL"]'
        ).first

        if input_box.count() == 0:
            input_box = page.locator(
                'input[placeholder*="Paste"]'
            ).first

        if input_box.count() == 0:
            raise RuntimeError(
                "Campo para colar a URL não encontrado."
            )

        print("Colando URL completa...")

        input_box.fill(youtube_url)

        # -------------------------------------------------
        # 4. CLICAR EM GET FREE TRANSCRIPT
        # -------------------------------------------------

        button = page.get_by_text(
            "Get Free Transcript",
            exact=False
        ).first

        if button.count() == 0:
            button = page.get_by_text(
                "Get Transcript",
                exact=False
            ).first

        if button.count() == 0:
            raise RuntimeError(
                "Botão Get Free Transcript não encontrado."
            )

        print("Solicitando transcrição...")

        button.click()

        # -------------------------------------------------
        # 5. ESPERAR A TRANSCRIÇÃO
        # -------------------------------------------------

        page.wait_for_timeout(8000)

        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=30000
            )
        except Exception:
            pass

        print("Página de resultado carregada.")

        # -------------------------------------------------
        # 6. EXTRAIR TEXTO DA PÁGINA
        # -------------------------------------------------

        body_text = page.locator("body").inner_text()

        if len(body_text) < 500:
            raise RuntimeError(
                "A página retornou pouco conteúdo. "
                "A transcrição pode não ter sido gerada."
            )

        # Procura blocos grandes que provavelmente contenham
        # a transcrição.
        candidates = []

        selectors = [
            "textarea",
            "pre",
            '[class*="transcript"]',
            '[id*="transcript"]',
            "article",
            "main",
        ]

        for selector in selectors:
            elements = page.locator(selector)

            for i in range(elements.count()):
                try:
                    text = elements.nth(i).inner_text().strip()

                    if len(text) > 1000:
                        candidates.append(text)

                except Exception:
                    pass

        # Se não encontrou bloco específico,
        # usa o texto completo da página.
        if candidates:
            transcript = max(
                candidates,
                key=len
            )
        else:
            transcript = body_text

        # -------------------------------------------------
        # 7. VALIDAÇÃO
        # -------------------------------------------------

        if len(transcript) < 1000:
            raise RuntimeError(
                "Transcrição não encontrada ou muito curta."
            )

        # -------------------------------------------------
        # 8. SALVAR RESULTADO
        # -------------------------------------------------

        output = (
            f"VIDEO_ID: {video_id}\n"
            f"URL: {youtube_url}\n\n"
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
