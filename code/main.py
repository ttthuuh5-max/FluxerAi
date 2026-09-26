import os
import sys
import time
import threading

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(CODE_DIR)
for _p in (ROOT_DIR, CODE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config.settings import HOST, PORT, DEBUG, OPEN_BROWSER, DATA_DIR, SPLASH, SPLASH_FILE, SPLASH_WAIT

# ЗАСТАВКА + проверка библиотек. Должна идти ДО импорта Flask и остального кода:
# если чего-то не хватает, здесь оно докачается, а страница покажет «Скачиваем библиотеки…».
import p00_boot
p00_boot.start(host=HOST, port=PORT, splash=SPLASH, splash_file=SPLASH_FILE, open_browser_flag=OPEN_BROWSER)

from p01_app import app
import p03_storage
import p04_logs
import p05_keys
import p06_chats
import p07_prompts
import p08_agents
import p09_settings_models
import p16_usage
import p10_providers
import p11_media
import p12_tools
import p13_routing
import p14_chat
import p15_ide
import p17_local_models
import p18_video
import p19_telegram
import p20_discord
import p02_frontend

def _open_browser_when_ready(url, delay=1.0):
    p00_boot.open_browser(url, delay=delay)

def main():
    url = f"http://localhost:{PORT}"

    print("=" * 60)
    print("FluxerAi server")
    print(f"Открой в браузере: {url}")
    print("Провайдеры: Google, OpenAI, Anthropic, OpenRouter, Mistral, xAI,")
    print("Grokified, DeepSeek, Groq, Perplexity, Together AI, Cohere, Cloudflare")
    print("Ключи вводятся в интерфейсе (панель справа) — по несколько на провайдера")
    if p18_video.ffmpeg_available():
        print("Монтаж видео (ffmpeg): доступен — модель может вызывать call_tool: video_edit")
    else:
        print("Монтаж видео (ffmpeg): НЕ найден в PATH — инструмент video_edit отключён.")
        print("  Установите ffmpeg (https://ffmpeg.org/download.html) и перезапустите сервер, чтобы включить.")
    print(f"Данные (ключи/чаты/промты) хранятся в: {DATA_DIR}")
    print("=" * 60)

    # Если заставка уже открыла браузер — второй раз не открываем.
    if OPEN_BROWSER and not os.environ.get("WERKZEUG_RUN_MAIN") and not p00_boot.browser_opened():
        threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()

    # Ждём, пока заставка доиграет, и отдаём порт настоящему серверу.
    # Ждать браузер имеет смысл только если мы сами его открываем.
    p00_boot.handoff(client_wait=SPLASH_WAIT if OPEN_BROWSER else 0)

    app.run(host=HOST, port=PORT, debug=DEBUG)

if __name__ == "__main__":
    main()
