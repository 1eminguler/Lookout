"""Folds the many category names sites use ("staj", "Stajyer", "Internship", "Full stack"...) into a few groups.

Readers keep the category text as the site wrote it; the Feed filters and counts use these groups instead.
"""
import re
from typing import Optional

INTERNSHIP = "Internship"
TALENT = "Young talent & scholarships"
EVENTS = "Hackathons & events"
SOFTWARE = "Software & engineering"
BUSINESS = "Business & operations"
OTHER = "Other jobs"

# Checked in order: the first group whose pattern matches wins ("Data/Analytics internship" is an internship).
_RULES = (
    (INTERNSHIP, r"sta[jy]|intern|praktikum|part[- ]?time [öo]ğrenci|[öo]ğrenci program"),
    (TALENT, r"gen[çc] yetenek|young talent|talent program|trainee|graduate|bursiyer|\bburs\b|scholarship|grant|hibe|"
             r"fellowship|\bprogram(me)?\b|yetenek program"),
    (EVENTS, r"hackathon|ideathon|datathon|codeathon|etkinlik|event|yar[ıi][şs]ma|competition|challenge|"
             r"meetup|conference|konferans|summit|bootcamp|workshop|webinar"),
    (SOFTWARE, r"yaz[ıi]l[ıi]m|software|developer|geli[şs]tirici|full[ -]?stack|back[ -]?end|front[ -]?end|devops|\bsre\b|"
               r"\bai\b|\bml\b|machine learning|yapay zeka|data|veri|analitik|analytics|\bbi\b|embedded|g[öo]m[üu]l[üu]|"
               r"\bqa\b|test|ui ?/ ?ux|\bux\b|web|mobile|mobil|android|ios|cloud|bulut|security|g[üu]venlik|"
               r"information technology|\bit\b|bili[şs]im|teknoloji|technology|hardware|donan[ıi]m|robot|"
               r"electric|elektri|electronic|elektronik|mechanic|makine|m[üu]hendis|engineer|\bar ?[&-]? ?ge\b|r ?& ?d|"
               r"research|ara[şs]t[ıi]rma|scientist"),
    (BUSINESS, r"sales|sat[ıi][şs]|marketing|pazarlama|brand|marka|finan|accounting|muhasebe|operation|operasyon|"
               r"logistic|lojistik|purchas|sat[ıi]n alma|product|[üu]r[üu]n|support|destek|customer|m[üu][şs]teri|"
               r"human resources|insan kaynak|recruit|management|y[öo]netim|y[öo]neti[şs]im|strateji|strategy|"
               r"business|i[şs] geli[şs]tirme|analist|analyst|assistant|asistan|legal|hukuk|publishing|creative|tasar[ıi]m|design"),
)
_COMPILED = tuple((name, re.compile(pattern, re.I)) for name, pattern in _RULES)
GROUPS = (INTERNSHIP, TALENT, EVENTS, SOFTWARE, BUSINESS, OTHER)


def _fold(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


def group(raw: Optional[str]) -> str:
    """The Feed group for a category as a site wrote it."""
    text = _fold(str(raw or "").strip())
    if not text:
        return OTHER
    for name, pattern in _COMPILED:
        if pattern.search(text):
            return name
    return OTHER
