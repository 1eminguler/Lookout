"""Eligibility profiles, listing evidence, and conservative local checks."""
import hashlib
import json
import re
import unicodedata
from datetime import date


PROFILE_FIELDS = (
    "class_year", "graduation_date", "mandatory_internship", "school_insurance",
    "duration_weeks", "period", "cities", "remote_ok", "english_level",
    "department", "work_permit", "other",
)
LISTING_FIELDS = (
    "title", "org", "category", "location", "deadline", "deadline_raw",
    "start_date", "summary", "extra", "url",
)
VERDICTS = ("eligible", "unclear", "ineligible")
RESULTS = ("ok", "unclear", "fail")
LEVELS = ("none", "A1", "A2", "B1", "B2", "C1", "C2")
_YES_NO = ("yes", "no")
_SENTENCE_BREAK = re.compile(r"\r?\n+|(?<=[!?])\s+|(?<!\d\.)(?<=\.)\s+")
_MARKUP = re.compile(r"<[/!?a-z][^>]*>|\[[^\]]+\]\([^)]+\)", re.I)


def normalize_profile(value):
    if not isinstance(value, dict) or value.keys() - set(PROFILE_FIELDS):
        raise ValueError("Invalid eligibility profile fields")
    profile = {}
    for key in PROFILE_FIELDS:
        field = value.get(key)
        if field is None or field == "":
            continue
        if not isinstance(field, str):
            raise ValueError(f"{key} must be text")
        field = field.strip()
        if not field:
            continue
        if key in ("mandatory_internship", "school_insurance", "remote_ok") and field not in _YES_NO:
            raise ValueError(f"{key} must be yes or no")
        if key == "class_year" and field not in ("1", "2", "3", "4", "5", "6", "final", "graduate"):
            raise ValueError("Invalid class year")
        if key == "english_level" and field not in LEVELS:
            raise ValueError("Invalid English level")
        if key == "duration_weeks" and not re.fullmatch(r"0|[1-9][0-9]{0,3}", field):
            raise ValueError("Available duration must be a whole number of weeks")
        if key == "graduation_date":
            try:
                if date.fromisoformat(field).isoformat() != field:
                    raise ValueError
            except ValueError:
                raise ValueError("Graduation date must be YYYY-MM-DD") from None
        profile[key] = field[:1000]
    return profile


def read_profile(raw):
    try:
        return normalize_profile(json.loads(raw))
    except (TypeError, ValueError):
        return {}


def evaluation_hash(profile, item):
    payload = [profile, [item.get(field) for field in LISTING_FIELDS]]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _plain(value):
    return isinstance(value, str) and not _MARKUP.search(value) and all(
        unicodedata.category(c) not in ("Cc", "Cf", "Cs") for c in value
    )


def _sentences(text):
    if isinstance(text, str):
        for part in _SENTENCE_BREAK.split(text):
            part = part.strip()
            if part and _plain(part):
                yield part


def validate(value, item):
    """Normalize an untrusted decision and accept evidence only from the listing."""
    if not isinstance(value, dict):
        return {"verdict": "unclear", "checks": []}
    verdict = value.get("verdict")
    verdict = verdict if verdict in VERDICTS else "unclear"
    raw_checks = value.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        return {"verdict": "unclear", "checks": []}
    evidence = {part for key in LISTING_FIELDS if key not in ("extra", "url")
                for part in _sentences(item.get(key))}
    checks = []
    for raw in raw_checks[:40]:
        raw = raw if isinstance(raw, dict) else {}
        criterion = raw.get("criterion")
        valid_criterion = _plain(criterion) and bool(criterion.strip())
        criterion = criterion.strip()[:120] if valid_criterion else "Requirement"
        quote = raw.get("quote")
        quote = quote.strip() if _plain(quote) else ""
        result = raw.get("result")
        if result not in RESULTS or quote not in evidence or not valid_criterion:
            result = "unclear"
        checks.append({"criterion": criterion, "result": result, "quote": quote[:300] if quote in evidence else ""})
    results = {check["result"] for check in checks}
    if (verdict == "eligible" and results != {"ok"}) or (verdict == "ineligible" and "fail" not in results):
        verdict = "unclear"
    return {"verdict": verdict, "checks": checks}


def _fold(text):
    text = unicodedata.normalize("NFKD", text.casefold().replace("ı", "i"))
    return "".join(c for c in text if not unicodedata.combining(c))


_REQUIRED = re.compile(
    r"\b(?:must|required|mandatory|only|requirements?|en az|sart\w*|zorunlu\w*|"
    r"gerek\w*|olmasi|sadece|yalnizca|\w+(?:mali|meli)(?:dir)?)\b"
)
_QUALIFIED = re.compile(
    r"\b(?:not|no|non|without|optional|preferred|preferably|or|if|unless|except|"
    r"degil\w*|gerekmez|aranmaz|olmadan|olmayan|olmamali\w*|tercih\w*|veya|yahut|haric|esnek|esdeger)\b"
    r"|\b(?:doesn|isn|aren|don)['’]t\b|\bya da\b|\band/or\b"
)
_YEAR_WORDS = {"first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5", "sixth": "6"}
_YEAR = re.compile(
    r"\b([1-6])\s*\.\s*sinif\b|\b([1-6])(?:st|nd|rd|th)[ -]year\b"
    r"|\b(first|second|third|fourth|fifth|sixth)[ -]year\b"
)
_FINAL = re.compile(r"\b(?:son sinif|final[ -]year)\b")
_STUDENT_REQUIREMENT = re.compile(
    r"\brequirements?\s*:|\b(?:must (?:currently )?be|(?:are|is) required|students only|"
    r"only .+ (?:may apply|eligible|accepted)|"
    r"ogrencisi ol(?:mak|mali\w*|masi)|sinifta ol(?:mak|mali\w*|masi)|ogrencileri basvurabilir)\b"
)
_GRADUATE = re.compile(
    r"\b(?:mezun(?:lar|u)?|graduates?|graduated)\b"
)
_DURATION = re.compile(
    r"\b(?:en az|min(?:imum)?(?: of)?|at least)\s+(\d{1,3})\s+(is gunu|business days?|working days?|hafta|weeks?)\b"
)
_DURATION_CONTEXT = re.compile(
    r"\b(?:internship|interns?|placements?|staj\w*|available|availability|commit\w*|calis\w*|sure\w*)\b"
)
_SCHOOL_COVERAGE = re.compile(
    r"\b(?:school|university) (?:must |should |will )?(?:cover|provide)\w* (?:\w+ ){0,3}(?:insurance|sgk)\b"
    r"|\b(?:insurance|sgk) (?:\w+ ){0,5}(?:covered|provided) by (?:the |your )?(?:school|university)\b"
    r"|\b(?:sgk|sigorta\w*) (?:\w+ ){0,3}(?:okul\w*|universite\w*) tarafindan (?:sagla\w*|karsila\w*)\b"
)
_REMOTE = re.compile(r"\b(?:remote|hybrid|uzaktan|hibrit)\b")
_CITIES = frozenset((
    "adana", "ankara", "antalya", "bursa", "canakkale", "denizli", "eskisehir", "gaziantep", "istanbul",
    "izmir", "kayseri", "kocaeli", "konya", "mersin", "mugla", "sakarya", "samsun", "tekirdag", "trabzon",
))


def precheck(profile, item):
    checks = []

    def add(criterion, matches, quote):
        result = "unclear" if matches is None else ("ok" if matches else "fail")
        check = {"criterion": criterion, "result": result, "quote": quote[:300]}
        if check not in checks:
            checks.append(check)

    year = profile.get("class_year")
    for quote in dict.fromkeys(q for field in ("title", "summary") for q in _sentences(item.get(field))):
        text = _fold(quote).lstrip("-*• ")
        if _QUALIFIED.search(text):
            continue
        match = _DURATION.search(text)
        if match and (match.start() == 0 or _DURATION_CONTEXT.search(text)):
            weeks = profile.get("duration_weeks")
            days = int(match[1]) * (5 if match[2] in ("hafta", "week", "weeks") else 1)
            add("Available duration", int(weeks) * 5 >= days if weeks else None, quote)
        student_requirement = bool(_STUDENT_REQUIREMENT.search(text))
        if not student_requirement and not _REQUIRED.search(text) and not re.search(
            r"\b(?:mezunu?|aday\w*) olmak\b", text
        ):
            continue
        years = {a or b or _YEAR_WORDS[c] for a, b, c in _YEAR.findall(text)}
        # Skip ranges, even when only one endpoint includes the word "year" or "sınıf".
        if re.search(r"\b[1-6](?:st|nd|rd|th|\.)?\s*[-–]\s*[1-6]", text):
            years.clear()
        if student_requirement and len(years) == 1 and not _FINAL.search(text) and not _GRADUATE.search(text):
            add("Class year", year in years if year and year != "final" else None, quote)
        elif student_requirement and _FINAL.search(text) and not years and not _GRADUATE.search(text):
            add("Final-year student", True if year == "final" else False if year == "graduate" else None, quote)
        elif _GRADUATE.search(text) and not years and not _FINAL.search(text) and re.search(
            r"\b(?:only (?:recent |new )?graduates?|must (?:be (?:a |recent )?graduate|have graduated)|"
            r"(?:mezun|mezunu) ol(?:mak|mali\w*|masi)|mezunlar basvurabilir)\b", text
        ):
            add("Graduation", year == "graduate" if year else None, quote)
        if text.strip(" .:") == "zorunlu staj" or re.search(
            r"\b(?:zorunlu staj(?:i|iniz)? (?:olan|olmal\w*|olmasi|yapmasi|yapacak|sart\w*)|"
            r"(?:mandatory|compulsory) internship (?:is required|only)|"
            r"only (?:students|applicants|candidates) with (?:a )?(?:mandatory|compulsory) internship|"
            r"(?:must|required to) (?:complete|undertake|do|have) (?:a )?(?:mandatory|compulsory) internship)\b",
            text,
        ):
            mandatory = profile.get("mandatory_internship")
            add("Mandatory internship", mandatory == "yes" if mandatory else None, quote)
        if _SCHOOL_COVERAGE.search(text):
            insurance = profile.get("school_insurance")
            add("School insurance", insurance == "yes" if insurance else None, quote)
        if re.search(r"\b(?:english|ingilizce)\b", text) and re.search(
            r"\b(?:know\w*|speak\w*|fluen\w*|proficien\w*|level|bil\w*|seviye\w*|duzey\w*|[abc][12])\b"
            r"|\b(?:english is required|ingilizce (?:sart\w*|gere\w*))\b", text
        ):
            level = profile.get("english_level")
            threshold = re.search(r"\b(?:en az |at least |minimum(?: of)? )([abc][12])\b", text)
            matches = None
            if level == "none":
                matches = False
            elif level and threshold:
                matches = LEVELS.index(level) >= LEVELS.index(threshold[1].upper())
            add("English", matches, quote)

    description = _fold(" ".join(item.get(field) or "" for field in ("title", "summary")))
    locations = list(_sentences(item.get("location")))
    if len(locations) == 1:
        quote = locations[0]
        location = _fold(quote)
        if location in ("remote", "fully remote", "uzaktan", "tamamen uzaktan") and not re.search(
            r"\b(?:on[ -]?site|office|hybrid|ofis\w*|hibrit)\b", description
        ):
            remote = profile.get("remote_ok")
            add("Remote work", remote == "yes" if remote else None, quote)
        elif not _REMOTE.search(description):
            # A single structured city is evidence of a location. A missing city
            # preference is unknown, and hybrid or multi-city values need review.
            city = re.fullmatch(r"([a-z]+)(?:,\s*(?:turkiye|turkey))?", location)
            allowed = {_fold(c.strip()) for c in re.split(r"[,;\n]", profile.get("cities", "")) if c.strip()}
            if city and (city[1] in _CITIES or city[1] in allowed):
                add("Work city", city[1] in allowed if allowed else None, quote)
    return checks


def combine(checks, agent=None):
    verdict = agent["verdict"] if agent else "unclear"
    merged = list(checks)
    if agent:
        resolved_locally = {check["criterion"].casefold() for check in checks if check["result"] != "unclear"}
        resolved_by_agent = {check["criterion"].casefold() for check in agent["checks"] if check["result"] != "unclear"}
        merged = [check for check in checks if check["result"] != "unclear"
                  or check["criterion"].casefold() not in resolved_by_agent]
        merged.extend(check for check in agent["checks"] if check["criterion"].casefold() not in resolved_locally)
    if any(check["result"] == "fail" for check in checks):
        verdict = "ineligible"
    elif verdict == "ineligible" and not any(check["result"] == "fail" for check in merged):
        verdict = "unclear"
    elif verdict == "eligible" and any(check["result"] != "ok" for check in merged):
        verdict = "unclear"
    return {"verdict": verdict, "checks": merged}
