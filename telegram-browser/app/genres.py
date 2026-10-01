from __future__ import annotations


GENRE_ALIASES: dict[str, str] = {
    "דוקו": "דוקומנטרי",
    "תעודי": "דוקומנטרי",
    "הסטוריה": "היסטוריה",
    "הרפתקאה": "הרפתקאות",
    "מוסיקה": "מוזיקה",
    "רומנטי": "רומנטיקה",
    "דרמה/סרט רומנטי ‧ 95 דקות": "דרמה",
}


def normalize_genre(value: str) -> str:
    value = " ".join(value.strip().split())

    return GENRE_ALIASES.get(
        value,
        value,
    )


def normalize_genres(
    genres: list[str],
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for genre in genres:
        normalized = normalize_genre(
            genre
        )

        if not normalized:
            continue

        if normalized in seen:
            continue

        seen.add(normalized)
        result.append(normalized)

    return result
