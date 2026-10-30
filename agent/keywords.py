"""Extract topical keywords from a conference name and optional topics text."""

from __future__ import annotations
import re

# Words that are common in conference names but carry no topical meaning
_FILLER: frozenset[str] = frozenset({
    "international", "national", "annual", "biennial", "triennial",
    "conference", "congress", "symposium", "workshop", "seminar",
    "meeting", "summit", "forum", "colloquium", "proceedings",
    "advances", "research", "applications", "technologies", "science",
    "journal", "letters", "transactions",
    "on", "in", "of", "the", "and", "for", "with", "a", "an", "at",
    "new", "recent", "emerging", "applied", "joint", "special",
    "general", "world", "global", "regional", "european", "asian",
    "american", "african", "ieee", "acm", "acis", "springer", "elsevier",
})

# (canonical label, list of case-insensitive regex patterns)
_VOCAB: list[tuple[str, list[str]]] = [
    ("Artificial Intelligence",     [r"artificial\s+intelligence", r"\bA\.?I\b"]),
    ("Machine Learning",            [r"machine\s+learning"]),
    ("Deep Learning",               [r"deep\s+learning"]),
    ("Neural Networks",             [r"neural\s+network", r"\bneural\b"]),
    ("Natural Language Processing", [r"natural\s+language\s+processing", r"\bNLP\b"]),
    ("Large Language Models",       [r"large\s+language\s+model", r"\bLLM\b"]),
    ("Generative AI",               [r"generative\s+(?:AI|model|intelligence)", r"\bGenAI\b"]),
    ("Computer Vision",             [r"computer\s+vision"]),
    ("Image Processing",            [r"image\s+processing"]),
    ("Signal Processing",           [r"signal\s+processing"]),
    ("Robotics",                    [r"\brobotics?\b"]),
    ("Autonomous Systems",          [r"autonom(?:ous|y)\s+(?:system|vehicle|driving|agent)"]),
    ("Data Science",                [r"data\s+science"]),
    ("Data Mining",                 [r"data\s+mining"]),
    ("Big Data",                    [r"big\s+data"]),
    ("Data Analytics",              [r"data\s+analytics?"]),
    ("Databases",                   [r"\bdatabase", r"data\s+engineering"]),
    ("Information Retrieval",       [r"information\s+retrieval"]),
    ("Knowledge Management",        [r"knowledge\s+(?:management|representation|graph)"]),
    ("Semantic Web",                [r"semantic\s+web", r"\bontolog"]),
    ("Cybersecurity",               [r"cyber\s*security", r"information\s+security", r"network\s+security", r"\bsecurity\b"]),
    ("Cryptography",                [r"cryptograph", r"\bcrypto\b"]),
    ("Privacy",                     [r"\bprivacy\b"]),
    ("Blockchain",                  [r"\bblockchain\b", r"\bweb\s*3\b"]),
    ("Cloud Computing",             [r"cloud\s+computing"]),
    ("Distributed Systems",         [r"distributed\s+system"]),
    ("Edge Computing",              [r"edge\s+computing"]),
    ("Internet of Things",          [r"internet\s+of\s+things", r"\bIoT\b"]),
    ("Networking",                  [r"\bnetworking\b", r"computer\s+network"]),
    ("Wireless Communications",     [r"wireless\s+communication", r"\b5G\b", r"\b6G\b"]),
    ("Mobile Computing",            [r"mobile\s+computing", r"mobile\s+application"]),
    ("Embedded Systems",            [r"embedded\s+system"]),
    ("Human-Computer Interaction",  [r"human[\s\-]+computer\s+interaction", r"\bHCI\b", r"\bCHI\b"]),
    ("Computer Graphics",           [r"computer\s+graphics"]),
    ("Virtual Reality",             [r"virtual\s+reality", r"\bVR\b"]),
    ("Augmented Reality",           [r"augmented\s+reality", r"\bAR\b"]),
    ("Multimedia",                  [r"\bmultimedia\b"]),
    ("Software Engineering",        [r"software\s+engineering"]),
    ("Programming Languages",       [r"programming\s+language"]),
    ("Formal Methods",              [r"formal\s+method", r"formal\s+verification"]),
    ("Algorithms",                  [r"\balgorithms?\b"]),
    ("Optimization",                [r"\boptimiz(?:ation|ing)\b", r"\boptimis(?:ation|ing)\b"]),
    ("Operations Research",         [r"operations?\s+research"]),
    ("Control Systems",             [r"control\s+system", r"control\s+theory"]),
    ("Bioinformatics",              [r"\bbioinformatics?\b", r"computational\s+biology"]),
    ("Healthcare",                  [r"\bhealthcare\b", r"health\s+informatics", r"medical\s+imaging"]),
    ("Medicine",                    [r"\bmedic(?:al|ine)\b"]),
    ("Education",                   [r"\beducation\b", r"\be[\-\s]*learning\b"]),
    ("Finance",                     [r"\bfinance\b", r"\bfinancial\b", r"\bfintech\b"]),
    ("Smart Cities",                [r"smart\s+cit"]),
    ("Energy",                      [r"\benergy\b", r"\brenewable\b", r"\bsustainab"]),
    ("Computer Science",            [r"computer\s+science"]),
    ("Mathematics",                 [r"\bmathematic"]),
]

_COMPILED: list[tuple[str, list[re.Pattern[str]]]] = [
    (label, [re.compile(p, re.IGNORECASE) for p in patterns])
    for label, patterns in _VOCAB
]


def _fallback_keywords(name: str) -> str:
    """Extract meaningful words from the conference name when the vocabulary finds nothing.

    Strips year suffixes, edition markers, and filler words, then returns up to five
    significant tokens so the keywords field is never empty.
    """
    # Remove bare years (2024, 2025 …) and edition markers like (5th)
    cleaned = re.sub(r"\b(19|20)\d{2}\b", "", name)
    cleaned = re.sub(r"\b\d+\w*\b", "", cleaned)          # "5th", "3rd", …
    cleaned = re.sub(r"[()[\]{}<>]", " ", cleaned)

    tokens = re.split(r"[\s\-/,;:&|]+", cleaned)
    kept: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if not tok:
            continue
        lower = tok.lower()
        if lower in _FILLER:
            continue
        if len(tok) < 3:
            continue
        if lower not in seen:
            seen.add(lower)
            kept.append(tok)
        if len(kept) >= 5:
            break

    # Last-resort: use the raw name so the field is never empty
    return ", ".join(kept) if kept else name.strip()


def extract_keywords(name: str, topics: str = "") -> str:
    """Return a comma-separated string of domain keywords found in the conference name/topics.

    Always returns a non-empty string: if the vocabulary finds no match the function
    falls back to extracting meaningful words directly from the conference name.
    """
    text = f"{name} {topics}"
    found = [label for label, pats in _COMPILED if any(p.search(text) for p in pats)]
    if found:
        return ", ".join(found)
    return _fallback_keywords(name)
