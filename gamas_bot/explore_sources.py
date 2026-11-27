"""The academic source catalog for the curated Explore bank.

Every production Explore question must trace to a real, verifiable source —
a major textbook, an institutional fact sheet, or a reputable reference with
clear authorship. This module is that catalog: a small, hand-maintained list
of genuine publications and institutional resources, each with the metadata
the feed renders in its "منبع علمی" panel.

Rules enforced here (and by ``gamas_bot.explore_seed.validate_seed_bank``):

* no Wikipedia as the primary evidence source, no blogs, no SEO sites, no
  social media, and no AI-generated citations — every URL below is a real,
  stable landing page of the named work or institution;
* ``evidence_level`` is one of ``textbook`` (Tier 1: peer-reviewed works,
  systematic reviews, major textbooks, university course materials),
  ``institutional`` (Tier 2: WHO, CDC, CIA, NASA, UNESCO, Encyclopaedia
  Iranica, NCBI), or ``educational`` (Tier 3: reputable educational
  references with clear authorship — MDN, Britannica);
* ``source_url`` is always ``https`` with no embedded credentials, so the
  Mini App can open it safely (see ``is_safe_source_url``).

Adding a question later means adding one entry to ``gamas_bot.explore_bank``
with a ``source_key`` that resolves here — no React change, no migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class AcademicSource:
    """One verifiable source exactly as ``explore_questions`` stores it."""

    key: str
    source_type: str
    title: str
    author: str
    institution: str
    url: str
    year: str = ""
    section: str = ""
    evidence_level: str = "textbook"


#: The only evidence levels a source may claim. ``textbook`` is Tier 1,
#: ``institutional`` is Tier 2, ``educational`` is Tier 3 (see module docstring).
EVIDENCE_LEVELS: tuple[str, ...] = ("textbook", "institutional", "educational")

#: The curated catalog. Keys are stable: a seed question stores the resolved
#: fields, never the key, so renaming a key cannot corrupt stored rows.
SOURCES: tuple[AcademicSource, ...] = (
    # --- Tier 1: major textbooks / university course materials (OpenStax) ---
    AcademicSource(
        "openstax-biology", "textbook", "Biology 2e", "OpenStax",
        "Rice University", "https://openstax.org/details/books/biology-2e",
        "2018", "Chapters on cells, plants and organisms", "textbook",
    ),
    AcademicSource(
        "openstax-concepts-biology", "textbook", "Concepts of Biology", "OpenStax",
        "Rice University", "https://openstax.org/details/books/concepts-biology",
        "2018", "Chapters on ecology and the environment", "textbook",
    ),
    AcademicSource(
        "openstax-microbiology", "textbook", "Microbiology", "OpenStax",
        "Rice University", "https://openstax.org/details/books/microbiology",
        "2016", "Chapters on bacteria and fungi", "textbook",
    ),
    AcademicSource(
        "openstax-anatomy", "textbook", "Anatomy and Physiology 2e", "OpenStax",
        "Rice University",
        "https://openstax.org/details/books/anatomy-and-physiology-2e",
        "2019", "Chapters on the human body systems", "textbook",
    ),
    AcademicSource(
        "openstax-chemistry", "textbook", "Chemistry 2e", "OpenStax",
        "Rice University", "https://openstax.org/details/books/chemistry-2e",
        "2019", "Chapters on atoms, bonds and reactions", "textbook",
    ),
    AcademicSource(
        "openstax-physics", "textbook", "Physics", "OpenStax",
        "Rice University", "https://openstax.org/details/books/physics",
        "2017", "Chapters on mechanics, energy and light", "textbook",
    ),
    AcademicSource(
        "openstax-algebra", "textbook", "College Algebra 2e", "OpenStax",
        "Rice University", "https://openstax.org/details/books/college-algebra-2e",
        "2021", "Chapters on numbers, equations and functions", "textbook",
    ),
    AcademicSource(
        "openstax-astronomy", "textbook", "Astronomy 2e", "OpenStax",
        "Rice University", "https://openstax.org/details/books/astronomy-2e",
        "2017", "Chapters on the Sun and the solar system", "textbook",
    ),
    AcademicSource(
        "ncbi-mcb", "textbook", "Molecular Biology of the Cell",
        "Alberts et al.", "NCBI Bookshelf, National Library of Medicine",
        "https://www.ncbi.nlm.nih.gov/books/NBK21054/", "2002",
        "Chapters on cell structure and function", "textbook",
    ),
    # --- Tier 2: WHO fact sheets and topic pages ---
    AcademicSource(
        "who-hiv", "fact_sheet", "HIV and AIDS — Fact sheet", "World Health Organization",
        "World Health Organization",
        "https://www.who.int/news-room/fact-sheets/detail/hiv-aids",
        "", "Fact sheet: transmission and prevention", "institutional",
    ),
    AcademicSource(
        "who-anaemia", "fact_sheet", "Anaemia — Fact sheet", "World Health Organization",
        "World Health Organization",
        "https://www.who.int/news-room/fact-sheets/detail/anaemia",
        "", "Fact sheet: causes and iron deficiency", "institutional",
    ),
    AcademicSource(
        "who-diet", "fact_sheet", "Healthy diet — Fact sheet", "World Health Organization",
        "World Health Organization",
        "https://www.who.int/news-room/fact-sheets/detail/healthy-diet",
        "", "Fact sheet: salt, fats and nutrition", "institutional",
    ),
    AcademicSource(
        "who-vaccines", "fact_sheet", "Vaccines and immunization",
        "World Health Organization", "World Health Organization",
        "https://www.who.int/health-topics/vaccines-and-immunization",
        "", "Topic page: how vaccines work", "institutional",
    ),
    AcademicSource(
        "who-antibiotics", "fact_sheet", "Antimicrobial resistance — Fact sheet",
        "World Health Organization", "World Health Organization",
        "https://www.who.int/news-room/fact-sheets/detail/antimicrobial-resistance",
        "", "Fact sheet: antibiotics and bacteria", "institutional",
    ),
    AcademicSource(
        "who-heart", "fact_sheet", "Cardiovascular diseases (CVDs) — Fact sheet",
        "World Health Organization", "World Health Organization",
        "https://www.who.int/news-room/fact-sheets/detail/cardiovascular-diseases-(cvd)",
        "", "Fact sheet: risk factors", "institutional",
    ),
    AcademicSource(
        "who-climate", "fact_sheet", "Climate change and health — Fact sheet",
        "World Health Organization", "World Health Organization",
        "https://www.who.int/news-room/fact-sheets/detail/climate-change-and-health",
        "", "Fact sheet: greenhouse gases and health", "institutional",
    ),
    AcademicSource(
        "who-air", "fact_sheet", "Air pollution — Fact sheet", "World Health Organization",
        "World Health Organization",
        "https://www.who.int/news-room/fact-sheets/detail/air-pollution",
        "", "Fact sheet: air pollutants and health", "institutional",
    ),
    AcademicSource(
        "who-water", "fact_sheet", "Drinking-water — Fact sheet",
        "World Health Organization", "World Health Organization",
        "https://www.who.int/news-room/fact-sheets/detail/drinking-water",
        "", "Fact sheet: safe water and hydration", "institutional",
    ),
    # --- Tier 2: CDC topic pages ---
    AcademicSource(
        "cdc-handwashing", "topic_page", "Handwashing", "Centers for Disease Control and Prevention",
        "Centers for Disease Control and Prevention",
        "https://www.cdc.gov/handwashing/", "", "Topic page: hand hygiene", "institutional",
    ),
    AcademicSource(
        "cdc-antibiotics", "topic_page", "Antibiotic Use",
        "Centers for Disease Control and Prevention",
        "Centers for Disease Control and Prevention",
        "https://www.cdc.gov/antibiotic-use/", "", "Topic page: antibiotics and bacteria", "institutional",
    ),
    AcademicSource(
        "cdc-sleep", "topic_page", "Sleep", "Centers for Disease Control and Prevention",
        "Centers for Disease Control and Prevention",
        "https://www.cdc.gov/sleep/", "", "Topic page: sleep duration", "institutional",
    ),
    AcademicSource(
        "cdc-nutrition", "topic_page", "Nutrition", "Centers for Disease Control and Prevention",
        "Centers for Disease Control and Prevention",
        "https://www.cdc.gov/nutrition/", "", "Topic page: nutrients and energy", "institutional",
    ),
    AcademicSource(
        "cdc-vaccines", "topic_page", "Vaccines", "Centers for Disease Control and Prevention",
        "Centers for Disease Control and Prevention",
        "https://www.cdc.gov/vaccines/", "", "Topic page: how vaccines work", "institutional",
    ),
    # --- Tier 2: government / intergovernmental reference ---
    AcademicSource(
        "cia-factbook", "reference", "The World Factbook", "Central Intelligence Agency",
        "Central Intelligence Agency", "https://www.cia.gov/the-world-factbook/",
        "", "Country profiles, capitals and borders", "institutional",
    ),
    AcademicSource(
        "nasa", "reference", "NASA", "National Aeronautics and Space Administration",
        "National Aeronautics and Space Administration", "https://www.nasa.gov/",
        "", "Apollo program history", "institutional",
    ),
    AcademicSource(
        "unesco-whl", "reference", "World Heritage List", "UNESCO",
        "United Nations Educational, Scientific and Cultural Organization",
        "https://whc.unesco.org/en/list/", "", "Inscribed properties by country", "institutional",
    ),
    AcademicSource(
        "iranica", "encyclopedia", "Encyclopaedia Iranica", "Encyclopaedia Iranica Foundation",
        "Encyclopaedia Iranica Foundation", "https://www.iranicaonline.org/",
        "", "Articles on Iranian history and culture", "institutional",
    ),
    # --- Tier 3: reputable educational references with clear authorship ---
    AcademicSource(
        "mdn", "reference", "MDN Web Docs", "Mozilla", "Mozilla",
        "https://developer.mozilla.org/", "", "Web technology glossary and guides", "educational",
    ),
    AcademicSource(
        "britannica", "encyclopedia", "Encyclopaedia Britannica", "Encyclopaedia Britannica, Inc.",
        "Encyclopaedia Britannica, Inc.", "https://www.britannica.com/",
        "", "Articles on world history and general knowledge", "educational",
    ),
)

SOURCES_BY_KEY: dict[str, AcademicSource] = {source.key: source for source in SOURCES}


def get_source(key: str) -> AcademicSource | None:
    """The catalog source for *key*, or ``None`` when it does not exist."""
    return SOURCES_BY_KEY.get(str(key or "").strip())


def is_safe_source_url(url: str) -> bool:
    """Whether *url* is safe to hand to the Mini App as a source link.

    Only ``https`` URLs with a real host and no embedded credentials pass;
    anything else (``javascript:``, ``data:``, protocol-relative, userinfo)
    is rejected, so a stored source can never become an arbitrary link.
    """
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    return True


def validate_source(source: AcademicSource) -> list[str]:
    """Return every problem with *source* (empty = valid)."""
    errors: list[str] = []
    where = source.key or "<empty key>"
    if not source.key.strip():
        errors.append(f"{where}: empty source key")
    if not source.title.strip():
        errors.append(f"{where}: empty source title")
    if not source.institution.strip():
        errors.append(f"{where}: empty source institution")
    if not is_safe_source_url(source.url):
        errors.append(f"{where}: source URL must be https without credentials")
    if source.evidence_level not in EVIDENCE_LEVELS:
        errors.append(f"{where}: unknown evidence level {source.evidence_level!r}")
    if source.source_type.strip() == "":
        errors.append(f"{where}: empty source type")
    return errors


def validate_catalog() -> list[str]:
    """Return every problem in the whole catalog (empty = valid)."""
    errors: list[str] = []
    seen: set[str] = set()
    for source in SOURCES:
        errors.extend(validate_source(source))
        if source.key in seen:
            errors.append(f"{source.key}: duplicate catalog key")
        seen.add(source.key)
    return errors


def source_payload(source: AcademicSource | None) -> dict[str, str] | None:
    """The API shape of a source, or ``None`` when there is none.

    ``verified`` is true only for a complete, safe, known-tier source; the
    feed uses it to decide between "منبع علمی" and "در انتظار راستی‌آزمایی".
    """
    if source is None:
        return None
    if validate_source(source):
        return None
    return {
        "source_type": source.source_type,
        "source_title": source.title,
        "source_author": source.author,
        "source_institution": source.institution,
        "source_url": source.url,
        "source_year": source.year,
        "source_page_or_section": source.section,
        "evidence_level": source.evidence_level,
        "verified": "true",
    }


def row_source_payload(row: dict) -> dict[str, str] | None:
    """The API shape of a source stored on an ``explore_questions`` row.

    A row is verified only when it carries a title, an institution, a safe
    URL and a known evidence level; anything less is returned as ``None`` so
    the client never presents an unverified question as academic-verified.
    """
    title = str(row.get("source_title") or "").strip()
    institution = str(row.get("source_institution") or "").strip()
    url = str(row.get("source_url") or "").strip()
    evidence = str(row.get("evidence_level") or "").strip()
    if not title or not institution or not is_safe_source_url(url):
        return None
    if evidence not in EVIDENCE_LEVELS:
        return None
    return {
        "source_type": str(row.get("source_type") or ""),
        "source_title": title,
        "source_author": str(row.get("source_author") or ""),
        "source_institution": institution,
        "source_url": url,
        "source_year": str(row.get("source_year") or ""),
        "source_page_or_section": str(row.get("source_page_or_section") or ""),
        "evidence_level": evidence,
        "verified": "true",
    }
