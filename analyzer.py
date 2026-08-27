"""Анализ еды по фото/описанию: Claude API или бесплатные модели OpenRouter."""
import base64
import json
import re

import aiohttp
import anthropic

import config


class DemoModeError(Exception):
    """Ни один ИИ-ключ не задан — бот работает в демо-режиме."""


class OpenRouterError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message


SYSTEM = """Ты — опытный нутрициолог и холистический health-коуч, работающий по подходу \
Пола Чека (Paul Chek, «How to Eat, Move and Be Healthy»).

Твоя задача: по фото еды и/или текстовому описанию оценить приём пищи.

Как оценивать КБЖУ:
- Определи блюдо и ингредиенты. Вес порции оценивай по визуальным ориентирам: \
стандартная тарелка ~26 см, вилка/ложка, стакан ~250 мл, кружка ~300 мл.
- Считай реалистично и учитывай скрытые калории: масло при жарке, заправки, соусы, \
сахар в напитках, майонез.
- Если пользователь в подписи указал вес, состав или уточнения — это приоритетный \
источник, используй его.
- confidence: «высокая», если блюдо и порция хорошо видны; «средняя» — обычный случай; \
«низкая» — если состав или размер порции угадать сложно.
- В assumptions одним коротким предложением главные допущения (например: «порция ~300 г, \
жарка на 1 ст. л. масла»).

Оценка по Полу Чеку (chek_score от 1 до 10). Главный принцип Чека: цельная, настоящая \
еда — «если это сделала природа, а не фабрика — ешь».
Плюсы: свежие овощи и зелень, качественные белки (яйца, рыба, мясо, птица, субпродукты), \
полезные жиры (сливочное, оливковое, кокосовое масло, орехи, авокадо), цельные крупы, \
ферментированные продукты, сбалансированная тарелка (белок + овощи + жиры), домашняя еда.
Минусы: рафинированный сахар и белая мука, сладкие напитки и десерты, трансжиры и \
рафинированные растительные масла, фритюр, колбасы и переработанное мясо, фастфуд, \
ультра-обработанные продукты с длинным составом, искусственные добавки.
Шкала: 9–10 — цельная сбалансированная еда; 7–8 — хорошо, есть мелкие замечания; \
5–6 — средне, заметная доля обработанного; 3–4 — преимущественно обработанная еда; \
1–2 — ультра-обработанное: сладости, газировка, фастфуд.

chek_verdict — одно короткое предложение: что хорошо и/или плохо с точки зрения Чека.
chek_tip — один конкретный дружелюбный совет, как сделать этот приём пищи ближе к \
принципам Чека (или короткая похвала, если всё отлично).

Все тексты пиши по-русски, кратко и дружелюбно, обращайся на «ты». Если на фото или в \
тексте нет ни еды, ни напитков — верни is_food=false, а числовые поля заполни нулями."""

MEAL_SCHEMA = {
    "type": "object",
    "properties": {
        "is_food": {"type": "boolean", "description": "Есть ли на фото/в тексте еда или напиток"},
        "dish": {"type": "string", "description": "Короткое название блюда или приёма пищи по-русски"},
        "items": {
            "type": "array",
            "description": "Разбивка по компонентам, если их больше одного",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "grams": {"type": "number"},
                    "kcal": {"type": "number"},
                },
                "required": ["name", "grams", "kcal"],
            },
        },
        "total_grams": {"type": "number"},
        "total_kcal": {"type": "number"},
        "total_protein_g": {"type": "number"},
        "total_fat_g": {"type": "number"},
        "total_carbs_g": {"type": "number"},
        "confidence": {"type": "string", "enum": ["низкая", "средняя", "высокая"]},
        "assumptions": {"type": "string"},
        "chek_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "chek_verdict": {"type": "string"},
        "chek_tip": {"type": "string"},
    },
    "required": [
        "is_food", "dish", "total_grams", "total_kcal", "total_protein_g",
        "total_fat_g", "total_carbs_g", "confidence", "chek_score", "chek_verdict",
    ],
}

MEAL_TOOL = {
    "name": "report_meal",
    "description": "Структурированный отчёт об анализе приёма пищи",
    "input_schema": MEAL_SCHEMA,
}


# ---------------------------------------------------------------- нормализация

def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _normalize(data: dict) -> dict:
    """Приводит ответ модели к надёжному виду (числа, диапазоны, дефолты)."""
    out = dict(data or {})
    out["is_food"] = bool(out.get("is_food", True))
    out["dish"] = str(out.get("dish") or "Приём пищи")
    for k in ("total_grams", "total_kcal", "total_protein_g", "total_fat_g", "total_carbs_g"):
        out[k] = _f(out.get(k))
    score = int(_f(out.get("chek_score")) or 5)
    out["chek_score"] = max(1, min(10, score))
    if out.get("confidence") not in ("низкая", "средняя", "высокая"):
        out["confidence"] = "средняя"
    if not isinstance(out.get("items"), list):
        out["items"] = []
    out["chek_verdict"] = str(out.get("chek_verdict") or "")
    return out


def _extract_json(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели (в т.ч. из ```-блоков и лишнего текста)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    start = text.find("{")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    parsed = json.loads(text[start:i + 1])
                    if isinstance(parsed, dict):
                        return parsed
                    break
    raise ValueError("В ответе модели не нашёлся JSON")


# ---------------------------------------------------------------- Claude (Anthropic)

_anthropic_client: anthropic.AsyncAnthropic | None = None


def _client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic_client


def _extract_tool_input(resp) -> dict | None:
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "report_meal":
            return dict(block.input)
    return None


async def _analyze_anthropic(image_bytes: bytes | None, media_type: str, user_text: str) -> dict:
    content: list[dict] = []
    if image_bytes:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode(),
            },
        })
    content.append({"type": "text", "text": user_text + "\nВызови инструмент report_meal."})

    kwargs = dict(
        model=config.MODEL,
        max_tokens=2000,
        system=SYSTEM,
        tools=[MEAL_TOOL],
        messages=[{"role": "user", "content": content}],
    )
    resp = await _client().messages.create(**kwargs)
    data = _extract_tool_input(resp)
    if data is None:
        resp = await _client().messages.create(
            **kwargs, tool_choice={"type": "tool", "name": "report_meal"}
        )
        data = _extract_tool_input(resp)
    if data is None:
        raise RuntimeError("Модель не вернула структурированный ответ")
    return data


# ---------------------------------------------------------------- OpenRouter

OR_BASE = "https://openrouter.ai/api/v1"
_or_model_cache: str | None = None

# Порядок предпочтения семейств моделей при автоподборе.
_FAMILY_PREFERENCE = ["gemma", "gemini", "qwen", "-vl", "vision", "nemotron", "llama", "mistral"]


def _model_score(model: dict) -> tuple:
    mid = model.get("id", "")
    family = 0
    for rank, kw in enumerate(_FAMILY_PREFERENCE):
        if kw in mid:
            family = len(_FAMILY_PREFERENCE) - rank
            break
    return (family, model.get("context_length") or 0)


async def _pick_free_vision_model(session: aiohttp.ClientSession) -> str:
    """Выбирает у OpenRouter бесплатную модель с поддержкой изображений."""
    global _or_model_cache
    if _or_model_cache:
        return _or_model_cache
    try:
        async with session.get(f"{OR_BASE}/models") as r:
            payload = await r.json()
    except aiohttp.ClientError as e:
        raise OpenRouterError(0, f"не удалось получить список моделей: {e}") from e
    candidates = []
    for m in payload.get("data", []):
        mid = m.get("id", "")
        modalities = (m.get("architecture") or {}).get("input_modalities") or []
        if mid.endswith(":free") and "image" in modalities:
            candidates.append(m)
    if not candidates:
        raise OpenRouterError(
            404,
            "у OpenRouter сейчас нет бесплатных моделей с поддержкой фото — "
            "выбери модель вручную на openrouter.ai/models и впиши её в OPENROUTER_MODEL в .env",
        )
    _or_model_cache = max(candidates, key=_model_score)["id"]
    return _or_model_cache


async def _or_chat(session: aiohttp.ClientSession, body: dict) -> str:
    async with session.post(f"{OR_BASE}/chat/completions", json=body) as r:
        try:
            payload = await r.json()
        except Exception:  # noqa: BLE001
            payload = {"error": {"message": (await r.text())[:300]}}
        err = payload.get("error") if isinstance(payload, dict) else None
        if r.status != 200 or err:
            msg = (err or {}).get("message") or str(payload)[:300]
            raise OpenRouterError(r.status, str(msg))
        try:
            return payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise OpenRouterError(200, "пустой ответ модели") from None


async def _analyze_openrouter(image_bytes: bytes | None, media_type: str, user_text: str) -> dict:
    schema_text = json.dumps(MEAL_SCHEMA, ensure_ascii=False)
    system = (
        SYSTEM
        + "\n\nОтветь ОДНИМ валидным JSON-объектом без markdown и пояснений, строго по схеме:\n"
        + schema_text
    )
    content: list[dict] = []
    if image_bytes:
        b64 = base64.standard_b64encode(image_bytes).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{b64}"},
        })
    content.append({"type": "text", "text": user_text + "\nОтветь только JSON."})

    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "X-Title": "Food Chek Bot",
    }
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        model = config.OPENROUTER_MODEL
        if model == "auto":
            model = await _pick_free_vision_model(session)
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "max_tokens": 1500,
            "temperature": 0.2,
        }
        text = await _or_chat(session, body)
        try:
            return _extract_json(text)
        except ValueError:
            # Бесплатные модели иногда добавляют лишний текст — одна строгая повторная попытка.
            body["messages"].append({"role": "assistant", "content": text[:2000]})
            body["messages"].append({
                "role": "user",
                "content": "Повтори ответ СТРОГО одним JSON-объектом по схеме, без единого слова вне JSON.",
            })
            text = await _or_chat(session, body)
            return _extract_json(text)


# ---------------------------------------------------------------- генерация текста

async def _text_anthropic(system: str, user: str) -> str:
    resp = await _client().messages.create(
        model=config.MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()


async def _text_openrouter(system: str, user: str) -> str:
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "X-Title": "Food Chek Bot",
    }
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        model = config.OPENROUTER_MODEL
        if model == "auto":
            model = await _pick_free_vision_model(session)
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 2000,
            "temperature": 0.6,
        }
        return (await _or_chat(session, body)).strip()


async def generate_text(system: str, user: str) -> str:
    provider = config.ACTIVE_PROVIDER
    if provider == "demo":
        raise DemoModeError()
    if provider == "openrouter":
        return await _text_openrouter(system, user)
    return await _text_anthropic(system, user)


WORKOUT_SYSTEM = """Ты — тренер и холистический коуч по системе Пола Чека \
(Paul Chek, «How to Eat, Move and Be Healthy»).

Составь тренировку. Жёсткие условия:
- Инвентарь ТОЛЬКО: турник, брусья и собственный вес. Никаких гантелей, штанг, \
резинок и тренажёров.
- Упражнения: подтягивания и их варианты (разные хваты, негативы, австралийские), \
отжимания на брусьях и их регрессии, висы, уголок/подъёмы ног, отжимания от пола/брусьев, \
приседания и выпады с собственным весом, планка.

Структура ответа (строго в этом порядке, кратко):
1. 🧘 Настрой: 1–2 минуты дыхания животом (по Чеку) — одна строка.
2. 🔥 Разминка: 3–4 суставных упражнения, по одной строке.
3. 💪 Основная часть: 4–6 упражнений с подходами × повторами и отдыхом. Для каждого — \
регрессия в скобках, если упражнение пока сложное. Учитывай primal patterns Чека \
(тяга, жим, присед, наклон, ротация).
4. 🌊 Заминка working in: 2–3 минуты — дыхание/лёгкая растяжка, одна-две строки.

Не давай советов по технике, восстановлению или образу жизни — только сам план \
занятия. Рекомендации даёт тренер, не ты.

Правила: пиши по-русски, обращайся на «ты», дружелюбно и без воды. НЕ используй \
markdown-разметку (звёздочки, решётки) — только обычный текст, эмодзи и переносы строк. \
Уложись примерно в 250–350 слов. Учитывай профиль пользователя и его прошлую тренировку, \
если они указаны: чередуй акценты (день тяги / день жима / смешанный) и предлагай \
небольшую прогрессию."""


async def generate_workout(profile: dict | None, context: str = "") -> str:
    """Генерирует тренировку на турнике/брусьях по Чеку. Возвращает обычный текст."""
    parts = []
    if profile:
        bits = []
        if profile.get("sex"):
            bits.append(str(profile["sex"]).lower())
        if profile.get("age"):
            bits.append(f"{profile['age']} лет")
        if profile.get("weight_kg"):
            bits.append(f"вес {profile['weight_kg']:.0f} кг")
        if profile.get("goal"):
            bits.append(f"цель: {str(profile['goal']).lower()}")
        if bits:
            parts.append("Профиль: " + ", ".join(bits) + ".")
    if context:
        parts.append(context)
    parts.append("Составь тренировку на сегодня.")
    return await generate_text(WORKOUT_SYSTEM, "\n".join(parts))


BRIEF_SYSTEM = """Ты — AI-ассистент тренера по здоровью. Тренер ведёт клиента и получает \
от тебя рабочие брифы. Пиши по-русски, кратко, по делу, без воды.

Тебе дают данные клиента за неделю из разных источников: еда, вода, тренировки, \
самочувствие (энергия/настроение/стресс/либидо), БАДы, анализы, а иногда сон и \
готовность с кольца Oura. Каких-то источников может не быть — тогда просто не упоминай их.

Выдай ровно четыре блока (обычный текст, без markdown-разметки):

📊 ЧТО ПРОИСХОДИТ
3–4 строки: калории и БЖУ vs цель, качество еды (Чек), вода, тренировки, активность \
логирования; если есть — самочувствие, сон/готовность, отклонения в анализах.

🔗 СВЯЗЬ
Самое ценное: свяжи источники между собой, если видишь общий сценарий (например, \
«дефицит белка + низкий ферритин + просевшая готовность и энергия — одна причина»). \
Если данных для связок мало — напиши, каких данных не хватает, чтобы увидеть картину.

⚠️ НА ЧТО ОБРАТИТЬ ВНИМАНИЕ
2–3 конкретных пункта: провалы, риски, положительные сдвиги. Если клиент почти не \
логирует — это пункт №1.

💬 ЧЕРНОВИК СООБЩЕНИЯ КЛИЕНТУ
3–5 предложений от первого лица тренера: тепло, поддерживающе, с одним конкретным \
фокусом на следующую неделю. Без нотаций и чувства вины. Пиши так, будто пишет живой \
человек, без канцелярита и без подписи."""


SUPPL_SYSTEM = """Ты — грамотный нутрициолог. Пользователь перечисляет БАДы и добавки, \
которые принимает, и время приёма. Оцени набор.

Дай ответ по-русски, кратко, обычным текстом без markdown-разметки, в трёх блоках:

⚠️ ВОЗМОЖНЫЕ КОНФЛИКТЫ
Пары, которые мешают усвоению друг друга или которые не стоит принимать вместе \
(например: кальций мешает усвоению железа и цинка; кальций и магний конкурируют в больших \
дозах). Если конфликтов нет — так и напиши.

⏰ КОГДА ЛУЧШЕ ПРИНИМАТЬ
Короткие подсказки по времени и условиям приёма для тех добавок из списка, где это важно \
(жирорастворимые витамины A, D, E, K — с едой, содержащей жир; железо — натощак или с \
витамином C, отдельно от кальция и кофе; магний — вечером; витамины группы B — утром). \
Отметь, если текущее время приёма пользователя стоит изменить.

💡 ЗАМЕТКИ
1–2 общих замечания, если уместно (дублирование, суммарные дозы, что обсудить с врачом).

В конце одной строкой добавь: «Это справочная информация, а не назначение — дозировки \
и курсы согласуй с врачом.»"""


async def check_supplements(supplements: list[dict]) -> str:
    """Проверка совместимости набора БАДов. supplements: [{name, timing}]."""
    if not supplements:
        return "Список пуст — добавь БАДы, и я проверю их совместимость."
    listing = "\n".join(
        f"- {s['name']}" + (f" ({s['timing']})" if s.get("timing") else "")
        for s in supplements
    )
    return await generate_text(SUPPL_SYSTEM, "Набор добавок пользователя:\n" + listing)


# ---------------------------------------------------------------- анализы (лаборатория)

class LabProviderError(Exception):
    """PDF-анализы поддерживаются только через Claude (Anthropic)."""


LAB_SYSTEM = """Ты — медицинский ассистент, который аккуратно оцифровывает бланки анализов. \
По изображению или PDF бланка извлеки показатели строго через инструмент report_labs.

Правила:
- Извлеки КАЖДЫЙ числовой показатель с его значением, единицами и референсным интервалом \
из бланка, как он там указан.
- value — число (используй точку как десятичный разделитель). Если значение не число \
(например «отрицательно»), положи его в value_text, а value оставь пустым.
- ref_low / ref_high — границы нормы из бланка (числа), если указаны.
- flag: «низко», если значение ниже нормы; «высоко», если выше; иначе «норма». \
Если границы не даны — «норма».
- date — дата взятия/выполнения анализа с бланка в формате YYYY-MM-DD, если видна.
- panel — название панели/раздела (например «Биохимический анализ крови», «Гормоны»).
- Названия показателей — по-русски, как на бланке. Не выдумывай показатели, которых нет.
- Если это не бланк анализов — верни is_lab=false с пустым списком markers."""

LAB_SCHEMA = {
    "type": "object",
    "properties": {
        "is_lab": {"type": "boolean"},
        "panel": {"type": "string"},
        "date": {"type": "string", "description": "YYYY-MM-DD или пусто"},
        "markers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": ["number", "null"]},
                    "value_text": {"type": "string"},
                    "unit": {"type": "string"},
                    "ref_low": {"type": ["number", "null"]},
                    "ref_high": {"type": ["number", "null"]},
                    "flag": {"type": "string", "enum": ["низко", "норма", "высоко"]},
                },
                "required": ["name", "flag"],
            },
        },
    },
    "required": ["is_lab", "markers"],
}

LAB_TOOL = {"name": "report_labs", "description": "Оцифрованный бланк анализов",
            "input_schema": LAB_SCHEMA}


async def parse_labs(data: bytes, media_type: str) -> dict:
    """Разбирает бланк анализов (PDF или фото). Возвращает dict по LAB_SCHEMA."""
    provider = config.ACTIVE_PROVIDER
    if provider == "demo":
        raise DemoModeError()

    is_pdf = media_type == "application/pdf"
    if provider == "openrouter" and is_pdf:
        raise LabProviderError()

    b64 = base64.standard_b64encode(data).decode()

    if provider == "anthropic":
        if is_pdf:
            block = {"type": "document",
                     "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
        else:
            block = {"type": "image",
                     "source": {"type": "base64", "media_type": media_type, "data": b64}}
        content = [block, {"type": "text",
                           "text": "Оцифруй этот бланк анализов через инструмент report_labs."}]
        kwargs = dict(model=config.MODEL, max_tokens=4000, system=LAB_SYSTEM,
                      tools=[LAB_TOOL], messages=[{"role": "user", "content": content}])
        resp = await _client().messages.create(**kwargs)
        out = _extract_tool_input_named(resp, "report_labs")
        if out is None:
            resp = await _client().messages.create(
                **kwargs, tool_choice={"type": "tool", "name": "report_labs"})
            out = _extract_tool_input_named(resp, "report_labs")
        if out is None:
            raise RuntimeError("Модель не вернула данные бланка")
        return _normalize_labs(out)

    # OpenRouter, изображение бланка
    system = LAB_SYSTEM + ("\n\nОтветь ОДНИМ валидным JSON-объектом строго по схеме:\n"
                           + json.dumps(LAB_SCHEMA, ensure_ascii=False))
    content = [
        {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
        {"type": "text", "text": "Оцифруй бланк. Ответь только JSON."},
    ]
    headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "X-Title": "Chek Bot"}
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        model = config.OPENROUTER_MODEL
        if model == "auto":
            model = await _pick_free_vision_model(session)
        body = {"model": model, "max_tokens": 3000, "temperature": 0.1,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": content}]}
        text = await _or_chat(session, body)
        return _normalize_labs(_extract_json(text))


def _extract_tool_input_named(resp, name: str) -> dict | None:
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return dict(block.input)
    return None


def _normalize_labs(data: dict) -> dict:
    out = {"is_lab": bool(data.get("is_lab", True)),
           "panel": str(data.get("panel") or "Анализы"),
           "date": str(data.get("date") or "").strip(),
           "markers": []}
    for m in (data.get("markers") or []):
        if not isinstance(m, dict) or not m.get("name"):
            continue
        flag = m.get("flag")
        if flag not in ("низко", "норма", "высоко"):
            flag = "норма"
        out["markers"].append({
            "name": str(m["name"])[:120],
            "value": _f(m["value"]) if m.get("value") is not None else None,
            "value_text": str(m.get("value_text") or "")[:60],
            "unit": str(m.get("unit") or "")[:40],
            "ref_low": _f(m["ref_low"]) if m.get("ref_low") is not None else None,
            "ref_high": _f(m["ref_high"]) if m.get("ref_high") is not None else None,
            "flag": flag,
        })
    return out


LAB_INTERP_SYSTEM = """Ты — внимательный health-коуч (не врач). По результатам анализов дай \
пользователю понятный разбор по-русски, обычным текстом без markdown.

Структура:
🔎 ГЛАВНОЕ
2–4 показателя, которые вышли за норму или близки к границе, простыми словами — что это \
значит для самочувствия.

🥗 ОБРАЗ ЖИЗНИ
Что из питания, сна, активности и добавок (в рамках подхода «цельная еда, баланс») может \
поддержать эти показатели. Только lifestyle-уровень, без назначения лекарств и доз.

❓ ЧТО ОБСУДИТЬ С ВРАЧОМ
1–3 конкретных вопроса, которые стоит задать врачу по этим результатам.

Заверши строкой: «Это не диагноз и не назначение. Интерпретировать анализы и принимать \
решения должен врач.»"""


async def interpret_labs(markers: list[dict], history_note: str = "") -> str:
    lines = []
    for m in markers:
        val = m.get("value_text") or (f"{m['value']}" if m.get("value") is not None else "?")
        ref = ""
        if m.get("ref_low") is not None or m.get("ref_high") is not None:
            ref = f" (норма {m.get('ref_low', '')}–{m.get('ref_high', '')})"
        flag = m.get("flag") or ""
        mark = " ⚠️" if flag in ("низко", "высоко") else ""
        lines.append(f"- {m['name']}: {val} {m.get('unit', '')}{ref} — {flag}{mark}")
    body = "Результаты анализов:\n" + "\n".join(lines)
    if history_note:
        body += "\n\nДинамика: " + history_note
    return await generate_text(LAB_INTERP_SYSTEM, body)


async def generate_brief(client_name: str, week_data: str, coach: dict | None = None) -> str:
    """AI-бриф тренеру по клиенту за неделю."""
    parts = [f"Клиент: {client_name}."]
    if coach and coach.get("name"):
        parts.append(f"Тренера зовут {coach['name']}.")
    if coach and coach.get("methodology"):
        parts.append(f"Методика тренера: {coach['methodology']}")
    parts.append("Данные клиента за последние 7 дней:")
    parts.append(week_data)
    parts.append("Составь бриф.")
    return await generate_text(BRIEF_SYSTEM, "\n".join(parts))


# ---------------------------------------------------------------- общая точка входа

async def analyze_meal(
    image_bytes: bytes | None = None,
    media_type: str = "image/jpeg",
    caption: str | None = None,
    text: str | None = None,
) -> dict:
    """Возвращает нормализованный dict по схеме MEAL_SCHEMA."""
    provider = config.ACTIVE_PROVIDER
    if provider == "demo":
        raise DemoModeError()

    parts = []
    if caption:
        parts.append(f"Подпись пользователя к фото: «{caption}»")
    if text:
        parts.append(f"Пользователь описал еду текстом: «{text}»")
    parts.append("Проанализируй этот приём пищи.")
    user_text = "\n".join(parts)

    if provider == "openrouter":
        data = await _analyze_openrouter(image_bytes, media_type, user_text)
    else:
        data = await _analyze_anthropic(image_bytes, media_type, user_text)
    return _normalize(data)
