import requests
from bs4 import BeautifulSoup
import re
from pathlib import Path

CHANNEL = "https://www.youtube.com/@fabioadriano/streams"

headers = {
    "User-Agent": "Mozilla/5.0"
}

# 1. Abre a página Ao vivo
html = requests.get(CHANNEL, headers=headers).text

# 2. Procura os IDs dos vídeos
ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)

# remove duplicados mantendo ordem
videos = list(dict.fromkeys(ids))

if not videos:
    raise RuntimeError("Nenhum vídeo encontrado")

# primeiro vídeo da aba Ao vivo
video_id = videos[0]

# URL completa
youtube_url = f"https://www.youtube.com/live/{video_id}"

print("Vídeo:", youtube_url)

# 3. Envia a URL inteira ao YouTubeToTranscript
site = "https://youtubetotranscript.com/"

session = requests.Session()
page = session.get(site, headers=headers)

soup = BeautifulSoup(page.text, "html.parser")

form = soup.find("form")

if form is None:
    raise RuntimeError("Formulário não encontrado")

action = form.get("action") or site
method = form.get("method", "post").lower()

data = {}

for inp in form.find_all("input"):
    name = inp.get("name")
    if name:
        data[name] = inp.get("value", "")

# encontra o campo da URL
for key in data:
    if "url" in key.lower() or "youtube" in key.lower():
        data[key] = youtube_url

if method == "post":
    result = session.post(action, data=data, headers=headers)
else:
    result = session.get(action, params=data, headers=headers)

result.raise_for_status()

# 4. Extrai a transcrição
soup = BeautifulSoup(result.text, "html.parser")

transcript = None

for tag in soup.find_all(["textarea", "pre", "div"]):
    text = tag.get_text("\n", strip=True)
    if len(text) > 500:
        transcript = text
        break

if not transcript:
    raise RuntimeError("Transcrição não encontrada")

# 5. Salva
Path("ultima_live.txt").write_text(transcript, encoding="utf-8")

print("Transcrição salva")
