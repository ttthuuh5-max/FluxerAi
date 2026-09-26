# FluxerAi

[![License: AGPL v3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](LICENSE)

Локальный сервер-чат для нескольких ИИ-провайдеров. Раньше — один файл
`server-17.py` (6348 строк), теперь разделён на части.

## Запуск

```bash
pip install -r requirements.txt
python code/main.py          # или run.bat (Windows) / ./run.sh (Linux, macOS)
```
Откроется http://localhost:5000

## Структура

```
FluxerAi/
├── config/                     ← ПАПКА 1: настройки и данные
│   ├── settings.py             порт, пути, лимиты (правь здесь)
│   ├── frontend.html           страница (HTML/CSS/JS) — можно править без перезапуска
│   ├── splash.html             заставка запуска (молния → текст → 3 2 1) — тоже правится без перезапуска
│   └── data/                   ключи, чаты, промты, агенты, logs.db
│
├── tools/                      ← ПАПКА 3: разовые утилиты
│   └── import_echohive_prompts.py   разовая утилита: добавляет промты формата echohive42 в store_prompts.json
│
└── code/                       ← ПАПКА 2: код
    ├── main.py                 ТОЧКА ВХОДА — собирает части и запускает
    ├── p00_boot.py             заставка + проверка/докачка библиотек (только stdlib, работает без Flask)
    ├── p01_app.py              Flask + CORS
    ├── p02_frontend.py         отдача страницы
    ├── p03_storage.py          чтение/запись JSON
    ├── p04_logs.py             логи запросов (SQLite) + /api/logs
    ├── p05_keys.py             API-ключи + ротация + /api/keys
    ├── p06_chats.py            сохранённые чаты
    ├── p07_prompts.py          мои промты + магазин промтов
    ├── p08_agents.py           суб-агенты (до 8)
    ├── p09_settings_models.py  настройки + справочник моделей
    ├── p10_providers.py        вызовы 33 провайдеров (call_google, call_openai … + таблица OpenAI-совместимых)
    ├── p11_media.py            генерация фото / видео / аудио
    ├── p12_tools.py            инструменты call_tool
    ├── p13_routing.py          ротация ключей, поиск модели, /api/health
    ├── p14_chat.py             /api/chat, /api/title_chat
    ├── p15_ide.py              IDE: файлы workspace, локальный shell, cloud sandbox
    ├── p16_usage.py            учёт токенов: точные числа из поля usage ответа провайдера
    ├── p17_local_models.py     локальные GGUF-модели: скачивание с HF + запуск через llama.cpp
    └── p18_video.py            монтаж видео через ffmpeg: call_tool: video_edit (обрезка, склейка,
                                 текст поверх видео, сжатие, извлечение аудио, gif). Нужен ffmpeg в PATH —
                                 без него инструмент просто не регистрируется, остальное работает как обычно.
```

## Где что менять

| Хочу…                              | Файл                       |
|------------------------------------|----------------------------|
| сменить порт, лимиты, пути         | `config/settings.py`       |
| поправить интерфейс                | `config/frontend.html`     |
| поправить заставку запуска         | `config/splash.html` (вид), `code/p00_boot.py` (логика) |
| добавить провайдера                | см. раздел «Добавить провайдера» ниже |
| добавить инструмент для модели     | `code/p12_tools.py`        |
| поменять логику чата               | `code/p14_chat.py`         |
| поменять показ/учёт токенов        | `code/p16_usage.py` (бэкенд), `buildUsageLine` в `config/frontend.html` |
| поправить IDE (интерфейс/агент)    | `config/frontend.html` (блок «IDE») |
| поправить IDE (файлы/команды)      | `code/p15_ide.py`          |

Настройки через переменные окружения: `PORT`, `HOST`, `DEBUG=0`, `OPEN_BROWSER=0`, `SPLASH=0`, `SPLASH_WAIT`, `FLUXERAI_DATA_DIR`.

## Заставка при запуске

При старте открывается страница-заставка, и всё происходит по порядку:

1. **Молния** — вспышка и удар молнии.
2. **Сине-зелёный текст** — `FluxerAi` появляется по буквам, под ним печатается подпись.
3. **Статус** — либо «Скачиваем библиотеки…» (если чего-то не хватает, оно докачивается через `pip`,
   в подписи виден ход установки), либо «Всё готово».
4. **«Мы перекинем вас на сайт через 3 · 2 · 1»** — и страница сама переходит в чат.
   Клик или нажатие клавиши пропускает отсчёт.

Как это работает: `code/p00_boot.py` (только стандартная библиотека, поэтому он работает, даже если
`flask` ещё не установлен) первым занимает порт сервера и отдаёт `config/splash.html`. Пока страница
играет, он проверяет библиотеки, а `main.py` в это время импортирует приложение. Когда отсчёт закончен,
заставка отдаёт порт настоящему Flask, страница дожидается его и переходит на сайт.

- Библиотеки, которые проверяются и докачиваются: список `REQUIRED` в `code/p00_boot.py`
  (добавил зависимость в `requirements.txt` — добавь и туда). `psutil` необязателен и не докачивается.
- На системах с защитой PEP 668 (Debian/Ubuntu) установка автоматически повторяется с
  `--break-system-packages`. Не удалось скачать — заставка покажет ошибку и команду для ручной установки.
- Если браузер не подключился, заставка **не задерживает** запуск: с `OPEN_BROWSER=0`
  сервер стартует сразу, а по умолчанию — через `SPLASH_WAIT` секунд (15). Закрыли вкладку
  посреди заставки — сервер стартует примерно через 6 секунд.
- `SPLASH=0` полностью отключает заставку (докачка библиотек при этом остаётся, вывод — в консоль).
- В Termux браузер открывается через `termux-open-url`, если он есть.
- Цвета (`--blue`, `--green`) и тексты (`NAME`, `TAGLINE`) правятся прямо в `config/splash.html`.

## Один файл (`main.py`)

Весь проект можно носить как **один файл** `main.py`: код, заставка и магазин промтов лежат внутри него.

```bash
python main.py                      # запуск: сразу открывается заставка, потом чат
python main.py --extract [папка]    # распаковать исходники (по умолчанию ./FluxerAi)
python main.py --pack [папка] [файл]  # собрать одиночный файл из папки (по умолчанию ./FluxerAi -> ./main_new.py)
```

- **Заставка появляется сразу.** Порт занимается и браузер открывается на заставке в первые доли секунды
  после запуска, ещё до распаковки данных и загрузки приложения; всё остальное идёт за её спиной.
  В консоли при этом печатается ссылка (`[FluxerAi] Заставка: http://localhost:5000`) — на случай, если
  браузер не открылся сам (в Termux ссылку можно просто нажать).
- **Данные лежат рядом с файлом**, в папке `FluxerAi_data/`: ключи, чаты, промты, логи, скачанные модели и
  папка IDE. Другое место: `FLUXERAI_DATA_DIR=/путь`. Файл `main.py` можно заменить новой версией —
  данные останутся. При первом запуске в эту папку кладётся магазин промтов (24 МБ), дальше он не трогается.
- **Код** при каждом запуске распаковывается во временную папку и удаляется при выходе.
- **Ключи и чаты в файл не попадают**: `--pack` кладёт из `config/data/` только `store_prompts.json`,
  `prompts.json` и `settings.json`.
- Режим отладки (`DEBUG`) в одиночном файле выключен. Для разработки: `--extract`, правки,
  `python code/main.py`, затем `--pack`.

**Безопасность ключей.** `GET /api/keys` отдаёт ключи только странице, открытой с этого же
сервера (запросы с чужих сайтов получают 403), а папка `config/data/` (ключи, чаты, логи) по URL
не раздаётся. Сервер по умолчанию слушает `0.0.0.0` — если он доступен по сети, запускай с `HOST=127.0.0.1`.

## Магазин промтов

В магазине **92 готовых системных промта** (`config/data/store_prompts.json`), разбитых на 2 категории.
Это встроенная база: интерфейса для импорта, экспорта и удаления промтов больше нет.

- Поиск идёт по названию, тексту и ключевым словам (поле `keywords`, если оно есть у промта).
  Индекс строится один раз при старте сервера (`p07_prompts._build_store_index`).
- **Список отдаёт только превью** (первые ~320 символов), а полный текст подгружается по клику
  «Использовать» / «Сохранить как роль» через `GET /api/store_prompts/<id>`. Некоторые промты весят
  до 475 КБ, и без этого список из 40 карточек тянул бы ~3 МБ.
- API: `GET /api/store_prompts?q=&category=&limit=&offset=`, `GET /api/store_prompts/categories`,
  `GET /api/store_prompts/<id>`.

Чтобы изменить набор промтов, отредактируй `config/data/store_prompts.json` (формат:
`{"prompts": [{"id", "name", "content", "category", "source", "updated_at"}]}`) и перезапусти сервер.

**Одиночный `main.py`:** при первом запуске база копируется в `FluxerAi_data/` и дальше не
перезаписывается. Если обновил `main.py` с новой базой, удали старый `FluxerAi_data/store_prompts.json`.

## Провайдеры

Изначально 13 (google, openai, anthropic, openrouter, mistral, xai, deepseek, grokified,
groq, perplexity, together, cohere, cloudflare) + **20 OpenAI-совместимых**:

| Провайдер | id | Base URL | Модели (примеры) | Вложения |
|---|---|---|---|---|
| Cerebras | `cerebras` | `https://api.cerebras.ai/v1` | `llama-3.3-70b`, `llama3.1-8b` | — |
| SambaNova | `sambanova` | `https://api.sambanova.ai/v1` | `Meta-Llama-3.3-70B-Instruct`, `DeepSeek-V3-0324` | фото |
| Fireworks AI | `fireworks` | `https://api.fireworks.ai/inference/v1` | `accounts/fireworks/models/llama-v3p3-70b-instruct`, `accounts/fireworks/models/deepseek-v3` | фото |
| NVIDIA NIM | `nvidia` | `https://integrate.api.nvidia.com/v1` | `meta/llama-3.3-70b-instruct`, `nvidia/llama-3.1-nemotron-70b-instruct` | — |
| Hyperbolic | `hyperbolic` | `https://api.hyperbolic.xyz/v1` | `meta-llama/Llama-3.3-70B-Instruct`, `deepseek-ai/DeepSeek-V3` | фото |
| DeepInfra | `deepinfra` | `https://api.deepinfra.com/v1/openai` | `meta-llama/Llama-3.3-70B-Instruct`, `deepseek-ai/DeepSeek-V3` | фото |
| Novita AI | `novita` | `https://api.novita.ai/v3/openai` | `meta-llama/llama-3.3-70b-instruct`, `deepseek/deepseek-v3-0324` | — |
| Nebius AI Studio | `nebius` | `https://api.tokenfactory.nebius.com/v1` | `meta-llama/Llama-3.3-70B-Instruct`, `deepseek-ai/DeepSeek-V3` | фото |
| Moonshot AI (Kimi) | `moonshot` | `https://api.moonshot.ai/v1` | `kimi-k2-0905-preview`, `kimi-k2-turbo-preview` | — |
| Z.AI (Zhipu GLM) | `zai` | `https://api.z.ai/api/paas/v4` | `glm-4.6`, `glm-4.5` | — |
| Alibaba Qwen (DashScope) | `dashscope` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `qwen-max`, `qwen-plus` | фото |
| SiliconFlow | `siliconflow` | `https://api.siliconflow.com/v1` | `deepseek-ai/DeepSeek-V3`, `Qwen/Qwen2.5-72B-Instruct` | — |
| Lambda Inference | `lambda` | `https://api.lambdalabs.com/v1` | `llama3.3-70b-instruct-fp8`, `deepseek-v3-0324` | — |
| Featherless AI | `featherless` | `https://api.featherless.ai/v1` | `meta-llama/Llama-3.3-70B-Instruct`, `Qwen/Qwen2.5-72B-Instruct` | — |
| Hugging Face Router | `huggingface` | `https://router.huggingface.co/v1` | `meta-llama/Llama-3.3-70B-Instruct`, `deepseek-ai/DeepSeek-V3` | фото |
| Vercel AI Gateway | `vercel` | `https://ai-gateway.vercel.sh/v1` | `anthropic/claude-sonnet-4.6`, `openai/gpt-5.4-nano` | фото |
| Scaleway Generative APIs | `scaleway` | `https://api.scaleway.ai/v1` | `llama-3.3-70b-instruct`, `deepseek-r1-distill-llama-70b` | — |
| AI21 Labs (Jamba) | `ai21` | `https://api.ai21.com/studio/v1` | `jamba-large`, `jamba-mini` | — |
| Upstage (Solar) | `upstage` | `https://api.upstage.ai/v1/solar` | `solar-pro2`, `solar-mini` | — |
| Inception Labs (Mercury) | `inception` | `https://api.inceptionlabs.ai/v1` | `mercury-2`, `mercury-coder` | — |

Ключ вводится в правой панели (поле по имени провайдера) → «Сохранить ключи на сервере».
Секция «Ключи API» и каждый провайдер в ней по умолчанию свёрнуты; у заполненных провайдеров
на заголовке виден счётчик «ключей: N». При открытии страницы сохранённые ключи подтягиваются
с сервера в поля (`GET /api/keys`). Очистил поле и сохранил — ключи этого провайдера удалятся на сервере.
Ключи на строку — сервер ротирует их и переходит к следующему при 401/403/429/5xx.
Список моделей подтягивается с провайдера живьём (`GET /models`); если не вышло — берётся
запасной список из `KNOWN_MODELS`. Провайдер можно не указывать: по имени модели он
подберётся автоматически.

Особенности:
- **Alibaba DashScope**: URL зависит от региона/workspace. По умолчанию международный
  `dashscope-intl…`; свой задай через `DASHSCOPE_BASE_URL=https://<WorkspaceId>.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`.
- **Vercel AI Gateway**: единый ключ ко многим провайдерам; имена моделей в формате `провайдер/модель`.
- **Nebius**: теперь называется Token Factory, домен `api.tokenfactory.nebius.com` (старый `studio.nebius.com` устарел).
- **Reasoning-модели**: если провайдер отдаёт `reasoning_content`/`reasoning`, мышление показывается свёрнутым блоком.

### Добавить ещё одного OpenAI-совместимого провайдера

1. `code/p10_providers.py` → строка в `OPENAI_COMPAT_ENDPOINTS`: `"id": ("Название", "https://…/v1/chat/completions")`.
   Если каталог моделей не по `<base>/models` — добавь URL в `OPENAI_COMPAT_MODELS_URLS`.
2. `code/p09_settings_models.py` → `KNOWN_MODELS["id"]` и `PROVIDER_ATTACHMENT_SUPPORT["id"]` (`{"image"}` или `set()`).
3. `code/p05_keys.py` → `_DEFAULT_KEY_PROVIDERS`.
4. `config/frontend.html` → блок `key-provider-block` с `id="keys-<id>"`, плюс `<id>` в массивы
   `KEY_PROVIDERS` и `CHAT_PROVIDERS_FOR_AGENTS` и цвет в `providerColors`.

Функция `call_<id>` создаётся автоматически, токены для неё считаются тоже автоматически (формат OpenAI). Для провайдеров с НЕ-OpenAI форматом пиши
отдельную `call_<id>` вручную (образец — `call_cohere`, `call_cloudflare`).
**Не забудь учёт токенов:** сразу после `resp.json()` вызови
`record_usage(<сырой usage из ответа>, "<стиль>")` (стили: `openai`, `anthropic`, `google`, `cohere` — см. `p16_usage.normalize_usage`).
Без этого ответ покажется без счётчика токенов.

## Локальные модели

Кнопка рядом с настройками (⚙ → квадратики) открывает панель «Локальные модели»:
поиск и скачивание GGUF-моделей с Hugging Face прямо в интерфейсе, с прогрессом
скачивания, свободной ОЗУ/диском и параметрами запуска на каждую модель.

**Поиск устроен как магазин промтов:** поле + чипы с типами задач (`text-generation`,
`image-to-text`, `image-text-to-text`, `feature-extraction`, `automatic-speech-recognition`,
`text-to-image` и др.). При открытии панели сразу видны самые популярные GGUF-модели.
- Название можно вводить **частично** — «llam» найдёт `llama-…` (HF ищет подстроку).
- Тип можно **выбрать чипом** или **написать**: `image-to-text` (целиком — выберется сам),
  `image to t`, «фото», «whisper», «vis» — чипы сузятся до подходящих, а если тип один,
  Enter его выбирает. Тип и название комбинируются: `qwen` + `text-generation`.
- Список идёт страницами по 100 моделей (по числу скачиваний), без общего потолка:
  «Следующие 100 →» заменяет список следующей сотней, «← Предыдущие 100» возвращает назад.
  У HF нет offset, поэтому сервер идёт по ссылке `Link: rel="next"` и запоминает её
  (`_hf_cursors` в `p17_local_models.py`) — следующая страница стоит один запрос.
- Список типов — константа `LM_TYPES` в `config/frontend.html`, туда же добавляются свои
  типы и синонимы для поиска.
- API: `GET /api/local-models/search?q=…&type=…&page=N&min_mb=…&max_mb=…&hf_start=…` → `results`, `page`, `page_size`, `has_next`, `next_hf_start`.
- Скачать можно модель любого типа, но в чате запускаются только текстовые.

**Скачивание** работает всегда (нужны только `flask`/`requests` из `requirements.txt`).
**Запуск** скачанной модели в чате требует ещё `llama-cpp-python` — она не входит
в `requirements.txt` намеренно (тяжёлая компилируемая зависимость, ставить по желанию):

```bash
pip install llama-cpp-python
# ускорение на GPU (пример — NVIDIA CUDA):
CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python --force-reinstall --no-cache-dir
# см. https://github.com/abetlen/llama-cpp-python#installation — там же Metal (Mac) и Vulkan.
```

**Termux (Android) и другие случаи, где `llama-cpp-python` не собирается.** Он не нужен:
если библиотеки нет, проект сам запускает `llama-server` (из llama.cpp) как фоновый
процесс на свободном локальном порту и общается с ним по HTTP. Параметры модели
(`n_ctx`, потоки, слои GPU) передаются ему аргументами. Бинарник ищется в `PATH`,
в `~/llama.cpp/build/bin/` и по переменной `LLAMA_SERVER_BIN`.

```bash
# Termux — готовый пакет, ничего не компилируется:
pkg install llama-cpp
# либо соберите сами:
pkg install git cmake clang make
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
cmake -B build && cmake --build build --config Release -j2
export LLAMA_SERVER_BIN=$PWD/build/bin/llama-server
```

На телефоне берите маленькие модели (1–3B, квантование Q4_K_M) и небольшой `n_ctx`.

Без обоих вариантов скачивание/удаление моделей и настройка параметров работают как обычно,
а при попытке отправить сообщение локальной модели чат вернёт понятную ошибку
с этой же командой для установки — сервер из-за отсутствия библиотеки не падает.

Как это устроено:
- Скачанные модели появляются в общем списке моделей (шапка чата) с провайдером
  `local` — выбираются и используются так же, как облачные, без ключа API.
- В памяти держится **только одна** загруженная модель одновременно: при выборе
  другой локальной модели предыдущая выгружается автоматически (см. `p17_local_models.py`,
  `_get_llama_for`) — так проще не упереться в ОЗУ на обычном компьютере.
- Параметры (`n_ctx`, слои на GPU, потоки CPU, температура) задаются в панели на
  каждую модель отдельно и применяются при следующей её загрузке; если модель
  сейчас в памяти, смена параметров выгружает её, чтобы не работать со старыми.
- **Несколько видеокарт.** По умолчанию `n_gpu_layers = -1` (все слои на GPU) и включено
  «Использовать все видеокарты»: если найдено 2+ карт, модель делится между ними
  (`split_mode=LAYER`, `tensor_split` — пропорционально VRAM каждой). Вручную долю можно
  задать полем «Распределение по картам» (например `3, 1`). Карты находятся через
  `nvidia-smi` (запасной путь — `torch`). Если раскладка по картам не удалась, модель
  загрузится без неё, а не упадёт. `-1` слоёв при сборке без GPU просто работает на CPU.
  Старые модели с прежним значением 0 слоёв при первом запуске переводятся на новый дефолт.
- **Фильтр размера.** В поиске есть «Размер модели, МБ: от … до …» (например 1–100).
  Сервер проверяет размеры `.gguf` файлов и оставляет репозитории, где есть файл в диапазоне.
  Первый такой поиск медленнее обычного (запрашиваются размеры файлов), повторные — из кеша.
- Чат-шаблон берётся из самого `.gguf` (большинство современных моделей несут его
  внутри); вложения (фото, PDF) локальные модели пока не читают.
- API: `/api/local-models/system` (ОЗУ/диск/CPU/видеокарты), `/search` и `/repo-files` (поиск на
  HF), `/download` + `/downloads` (скачивание с прогрессом, можно отменить),
  `/list`, `DELETE /<id>`, `POST /<id>/params`, `GET /runtime` (какая модель сейчас
  в памяти), `POST /<id>/unload`.

## Токены

Под каждым ответом ассистента показывается расход токенов — **точные числа от самого
провайдера** (поле `usage` в ответе API), а не оценка на глаз.

```
154 ток. ответ   запрос: 120   всего: 274
```

- Каждая `call_*` после ответа провайдера зовёт `record_usage(...)`. Форматы разных
  провайдеров (`prompt_tokens` / `input_tokens` / `promptTokenCount` …) приводятся к
  одному виду в `p16_usage.normalize_usage`.
- Сумма копится **за весь запрос**: раунды `call_tool`, вызовы суб-агентов и т.п. — это
  реальный расход, поэтому учитываются все обращения к модели. Неудачные попытки на
  другом ключе (401/429) токенов не добавляют. Число обращений показывается, если их больше одного.
- Токены «мышления» (Gemini `thoughtsTokenCount`, OpenAI `reasoning_tokens`) входят в
  токены ответа — так они и тарифицируются; отдельно показывается «из них мышление».
- Если провайдер `usage` не вернул — поле `usage` в ответе **отсутствует**, счётчик не
  рисуется (а не показывает выдуманный 0). Если не вернула часть обращений — рядом «⚠ неполно».
- Токены сохраняются в истории чата (`chats.json`) и в `logs.db`
  (колонки `input_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens`;
  старая база мигрирует сама, у старых записей — `NULL`). Отдаются в `/api/logs`.
- Ответ `/api/chat` теперь содержит `"usage": {input_tokens, output_tokens,
  reasoning_tokens, total_tokens, calls, partial}`.
- Не считаются: `/api/title_chat` (название чата) и генерация медиа (фото/видео/аудио) —
  это не ответ нейросети в чате.

## Порядок зависимостей

`p00 (заставка, первым, только stdlib) → p01 → p03 → p04/p05 → p06/p07 → p08 → p09 → p16 → p10 → p11 → p12 → p13 → p14 → p15 → p17 → p02`
(`p16_usage` не зависит от остальных частей — его используют `p10` и `p14`.
`p17_local_models` регистрирует провайдера `"local"` в том же словаре `PROVIDERS`,
что и `p10_providers` — импортируется после него; `p13_routing` обходится с ним без
ключа отдельной веткой в `call_with_key_rotation`/`/api/health`.)

Два места, где части ссылаются друг на друга «назад», сделаны ленивым импортом
внутри функции (`p08 ↔ p12`, `p12 ↔ p13`). `TOOLS_SYSTEM_PROMPT_ADDON`
обновляется на лету, поэтому `p14_chat` читает его как `p12_tools.TOOLS_SYSTEM_PROMPT_ADDON`,
а не копирует через `from … import`.

## IDE

Кнопка `>_` в шапке открывает полноэкранную IDE: файлы · редактор · терминал · ИИ-агент.

- Файлы лежат в `config/data/workspace` (создаётся при первом открытии). Другая папка: `IDE_WORKSPACE=/путь`.
- Агент использует модель, выбранную в шапке, и может создавать/править файлы и запускать команды.
- Режим «Облако» требует ключ Sandbox as a Service (поле в настройках → провайдер `aas`).
- ВНИМАНИЕ: в локальном режиме команды выполняются на твоём компьютере без ограничений.
  Если сервер доступен по сети, запускай с `HOST=127.0.0.1`.

## Разрешения агента в IDE

В панели агента рядом с «Отправить» — кнопка-флажок `⚑ N` (N = сколько прав включено).
Нажми — откроется окно с галочками:

| Галочка | Что разрешает |
|---|---|
| Выполнять команды | главный выключатель терминала для агента |
| Скачивание | curl, wget, git clone, скачивание моделей |
| Установка пакетов | pip, npm, apt, cargo… |
| Сеть / серверы | запуск сервера, ssh, ping, http-запросы |
| Удаление файлов | rm, git reset --hard, инструмент `<delete_file>` |
| sudo / права админа | sudo, chmod -R, chown -R |
| Сразу, без подтверждения | выключено → перед каждой командой диалог «Выполнить?» |

Сверху переключатель **Локально / Виртуалка** — тот же, что и в шапке IDE.
Настройки хранятся в браузере (`localStorage`, ключ `fluxer_ide_agent_perms`).

Права проверяются **на сервере** (`code/p15_ide.py`, `_check_perms`), а не только в промпте:
запрещённая команда не запустится, даже если модель попытается. Ручной терминал IDE не ограничивается.
Разрушительные команды (`rm -rf /`, `mkfs`, `shutdown`…) блокируются всегда, независимо от галочек.
