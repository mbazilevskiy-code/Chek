"""Расчёт дневных норм КБЖУ и суммирование дневника."""

ACTIVITIES = {
    "Минимальная (сидячая работа)": 1.2,
    "Лёгкая (1–3 тренировки в неделю)": 1.375,
    "Средняя (3–5 тренировок в неделю)": 1.55,
    "Высокая (6–7 тренировок в неделю)": 1.725,
}

GOALS = {
    "Похудеть": 0.85,       # дефицит ~15%
    "Поддерживать": 1.0,
    "Набрать": 1.10,        # профицит ~10%
}


def calc_targets(sex: str, age: int, height_cm: float, weight_kg: float,
                 activity: str, goal: str) -> dict:
    """Норма калорий по Миффлину — Сан-Жеору + разумные БЖУ."""
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if sex == "Мужчина" else -161)
    tdee = bmr * ACTIVITIES.get(activity, 1.375)
    kcal = tdee * GOALS.get(goal, 1.0)

    protein = weight_kg * (1.6 if goal == "Поддерживать" else 1.8)  # г/кг
    fat = kcal * 0.30 / 9                                           # ~30% калорий из жира
    carbs = max((kcal - protein * 4 - fat * 9) / 4, 30)

    return {
        "kcal_target": int(round(kcal / 10) * 10),
        "protein_target": int(round(protein / 5) * 5),
        "fat_target": int(round(fat / 5) * 5),
        "carb_target": int(round(carbs / 5) * 5),
        "bmr": int(bmr),
        "tdee": int(tdee),
    }


def day_totals(meals: list[dict]) -> dict:
    scores = [m["chek_score"] for m in meals if m.get("chek_score")]
    return {
        "n": len(meals),
        "kcal": sum(m["kcal"] or 0 for m in meals),
        "protein": sum(m["protein"] or 0 for m in meals),
        "fat": sum(m["fat"] or 0 for m in meals),
        "carbs": sum(m["carbs"] or 0 for m in meals),
        "chek": (sum(scores) / len(scores)) if scores else None,
    }


def water_target_ml(weight_kg: float | None) -> int:
    """Норма воды по Чеку: ~0,033 л на кг веса (округление до 50 мл)."""
    if not weight_kg:
        return 2000
    return int(round(weight_kg * 33 / 50) * 50)


def chek_day_verdict(avg: float) -> str:
    if avg >= 8.5:
        return "день по Чеку — образцовый, так держать! 🌟"
    if avg >= 7:
        return "хороший день по Чеку — в основном цельная еда."
    if avg >= 5.5:
        return "средне по Чеку — часть еды обработанная, есть куда расти."
    if avg >= 4:
        return "по Чеку слабовато — много обработанных продуктов."
    return "по Чеку совсем мимо — Пол бы расстроился 🙂 Завтра — цельная еда!"
