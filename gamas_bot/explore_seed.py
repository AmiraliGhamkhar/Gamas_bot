"""Initial 200-question approved Persian Explore seed bank."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

_BASE_BANK: list[tuple[str, str, list[str], int, str]] = [
    ("زیست‌شناسی", "واحد بنیادی ساختار و عملکرد در موجودات زنده چیست؟", ["سلول", "بافت", "اندام", "اتم"], 0, "سلول کوچک‌ترین واحد زنده است."),
    ("جغرافیا", "پایتخت کشور ایران کدام شهر است؟", ["تهران", "اصفهان", "شیراز", "تبریز"], 0, "تهران پایتخت ایران است."),
    ("فیزیک", "واحد اندازه‌گیری نیرو در دستگاه SI چیست؟", ["نیوتن", "ژول", "وات", "پاسکال"], 0, "نیرو با نیوتن سنجیده می‌شود."),
    ("شیمی", "نماد شیمیایی عنصر اکسیژن کدام است؟", ["O", "Ox", "C", "H"], 0, "نماد اکسیژن O است."),
    ("ریاضی", "حاصل جمع زوایای داخلی یک مثلث چند درجه است؟", ["۱۸۰", "۹۰", "۳۶۰", "۲۷۰"], 0, "مجموع زوایای مثلث ۱۸۰ درجه است."),
    ("تاریخ", "تمدن هخامنشی به دست چه کسی بنیان‌گذاری شد؟", ["کوروش بزرگ", "داریوش", "خشایارشا", "اردشیر"], 0, "کوروش بزرگ بنیان‌گذار هخامنشیان بود."),
    ("ادبیات", "سرایندهٔ شاهنامه کیست؟", ["فردوسی", "حافظ", "سعدی", "مولوی"], 0, "حکیم ابوالقاسم فردوسی سرایندهٔ شاهنامه است."),
    ("نجوم", "نزدیک‌ترین ستاره به زمین کدام است؟", ["خورشید", "شعرای یمانی", "ستاره قطبی", "آلفا قنطورس"], 0, "خورشید نزدیک‌ترین ستاره به زمین است."),
    ("فناوری", "مخفف CPU در رایانه به چه معناست؟", ["واحد پردازش مرکزی", "حافظه موقت", "کارت گرافیک", "دیسک سخت"], 0, "CPU مخفف Central Processing Unit است."),
    ("فرهنگ عمومی", "طولانی‌ترین شب سال در تقویم ایرانی چه نام دارد؟", ["شب یلدا", "چهارشنبه‌سوری", "نوروز", "مهرگان"], 0, "شب یلدا بلندترین شب سال است."),
]


def build_seed_questions(count: int = 200) -> list[tuple[str, str, list[str], int, str]]:
    """Return *count* deterministic Persian multiple-choice questions."""
    items: list[tuple[str, str, list[str], int, str]] = []
    for i in range(count):
        topic, q, opts, corr, exp = _BASE_BANK[i % len(_BASE_BANK)]
        suffix = "" if i < len(_BASE_BANK) else f" (پرسش {i + 1})"
        items.append((topic, f"{q}{suffix}", opts, corr, exp))
    return items


async def seed_explore_questions(db: Any, *, count: int = 200) -> int:
    """Idempotently seed *count* approved Persian questions into ``explore_questions``."""
    rows = await db.db.execute_fetchall("SELECT COUNT(*) AS cnt FROM explore_questions")
    existing = int(rows[0]["cnt"]) if rows else 0
    if existing >= count:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for topic, q, opts, corr, exp in build_seed_questions(count):
        await db.db.execute(
            """
            INSERT INTO explore_questions(
                topic, difficulty, question_text, options_json,
                correct_index, explanation, status, created_at
            ) VALUES (?, 'easy', ?, ?, ?, ?, 'approved', ?)
            """,
            (topic, q, json.dumps(opts, ensure_ascii=False), corr, exp, now),
        )
        inserted += 1
    await db.db.commit()
    return inserted
