import json
import os
import shutil
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_FILE = os.path.join(ROOT, "config", "data", "store_prompts.json")
ID_PREFIX = "ech10k_"
SOURCE = "echohive42/10-000-chatbot-prompts (MIT)"

GROUP_MARKERS = [
    ("Physics", "Наука и математика"),
    ("Software Development", "Программирование"),
    ("Mathematics", "Наука и математика"),
    ("Mechanical Engineering", "Инженерия"),
    ("Software Architecture", "Программирование"),
    ("Business Strategy", "Бизнес и карьера"),
    ("Finance", "Финансы и экономика"),
    ("Entrepreneurship", "Бизнес и карьера"),
    ("Marketing", "Маркетинг"),
    ("Economics (General)", "Финансы и экономика"),
    ("Project Planning", "Бизнес и карьера"),
    ("Education (General)", "Обучение"),
    ("Career Development", "Бизнес и карьера"),
    ("Personal Development", "Саморазвитие и общение"),
    ("Health and Wellness", "Здоровье"),
    ("Psychology (General)", "Психология и философия"),
    ("Linguistics", "Языки и лингвистика"),
    ("Philosophy (General)", "Психология и философия"),
    ("Sociology (General)", "История, культура и общество"),
    ("Geography (General)", "Природа и науки о Земле"),
    ("Sustainability (General)", "Экология и климат"),
    ("Architecture (General)", "Архитектура и дизайн"),
    ("Transportation (General)", "Транспорт, авиация и космос"),
    ("Hospitality", "Туризм и мероприятия"),
    ("Sports (General)", "Спорт и отдых"),
    ("Hobbies (General)", "Хобби и ремёсла"),
    ("Arts (General)", "Искусство, музыка и мода"),
    ("Literature (General)", "Писательство"),
    ("Languages (General)", "Языки и лингвистика"),
    ("World Cultures (General)", "История, культура и общество"),
    ("Food (General)", "Кулинария"),
    ("Lifestyle (General)", "Дом и образ жизни"),
    ("Wellness Practices", "Здоровье"),
    ("Self-Help Topics", "Саморазвитие и общение"),
    ("Relationships (Non-Erotic)", "Семья и отношения"),
    ("Technology Trends", "IT, ИИ и безопасность"),
    ("Computer Science (General)", "Программирование"),
    ("Machine Learning (Advanced)", "IT, ИИ и безопасность"),
    ("Digital Communication", "Маркетинг"),
    ("Online Communities", "Сообщества и НКО"),
    ("Human Resources (Advanced)", "Бизнес и карьера"),
    ("Nonprofit Management", "Сообщества и НКО"),
    ("Event Management (General)", "Туризм и мероприятия"),
    ("Media (General)", "Кино, медиа и развлечения"),
    ("Gaming (General)", "Игры"),
    ("Animation", "Кино, медиа и развлечения"),
    ("Children\u2019s Activities", "Семья и отношения"),
    ("Animals (General)", "Природа и науки о Земле"),
    ("Environmental Initiatives", "Экология и климат"),
    ("General Knowledge", "Обучение"),
    ("Library Science", "История, культура и общество"),
    ("Aviation (General)", "Транспорт, авиация и космос"),
    ("Cosmology (Non-Religious)", "Наука и математика"),
    ("Art History (General)", "Искусство, музыка и мода"),
    ("Etiquette (General)", "Саморазвитие и общение"),
    ("Publishing", "Писательство"),
    ("Freelancing (General)", "Бизнес и карьера"),
    ("Personal Finance (Advanced)", "Финансы и экономика"),
    ("Collecting (Advanced)", "Хобби и ремёсла"),
    ("Performing Arts (Advanced)", "Искусство, музыка и мода"),
    ("Wellness (Advanced)", "Здоровье"),
    ("Cooking Techniques", "Кулинария"),
    ("Personal Organization", "Саморазвитие и общение"),
]

def assign_groups(records):
    markers = dict(GROUP_MARKERS)
    pending = [m for m, _ in GROUP_MARKERS]
    current = None
    groups = []
    for r in records:
        cat = r["parent_category"]
        if pending and cat == pending[0]:
            current = markers[cat]
            pending.pop(0)
        if current is None:
            raise SystemExit("Файл начинается не с ожидаемой категории: %r" % cat)
        groups.append(current)
    if pending:
        raise SystemExit("Не встретились метки групп (файл другой версии?): %s" % pending[:5])
    return groups

def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    src = sys.argv[1]
    with open(src, "r", encoding="utf-8") as f:
        records = json.load(f)
    if not isinstance(records, list) or not records:
        raise SystemExit("Ожидался непустой JSON-массив")
    need = {"id", "parent_category", "subcategory", "system_message"}
    for i, r in enumerate(records):
        if not need <= set(r):
            raise SystemExit("Запись #%d без обязательных полей %s" % (i, need - set(r)))

    groups = assign_groups(records)

    with open(STORE_FILE, "r", encoding="utf-8") as f:
        store = json.load(f)
    prompts = store["prompts"]
    have_ids = {p.get("id") for p in prompts}
    have_texts = {(p.get("content") or "").strip() for p in prompts}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = skipped_id = skipped_text = skipped_empty = 0
    for r, group in zip(records, groups):
        pid = ID_PREFIX + str(r["id"])
        text = (r["system_message"] or "").strip()
        if not text:
            skipped_empty += 1
            continue
        if pid in have_ids:
            skipped_id += 1
            continue
        if text in have_texts:
            skipped_text += 1
            continue
        prompts.append({
            "id": pid,
            "name": "%s \u2014 %s" % (r["parent_category"].strip(), r["subcategory"].strip()),
            "content": text,
            "category": group,
            "updated_at": now,
            "source": SOURCE,
            "keywords": [k for k in (r.get("keywords") or []) if isinstance(k, str)],
        })
        have_ids.add(pid)
        have_texts.add(text)
        added += 1

    if added:
        shutil.copy2(STORE_FILE, STORE_FILE + ".bak")
        tmp = STORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STORE_FILE)

    print("Добавлено: %d | пропущено (уже есть id): %d | (тот же текст): %d | (пустые): %d"
          % (added, skipped_id, skipped_text, skipped_empty))
    print("Всего в магазине теперь: %d" % len(prompts))

if __name__ == "__main__":
    main()
