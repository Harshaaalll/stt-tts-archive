"""Sentiment-bucketed filler word classifier.

Classifies a user utterance into one of `positive`, `negative`, `neutral` using
per-language keyword lists. Negative is checked first because a refusal often
contains positive-sounding substrings (e.g. "नहीं करूंगा" contains "करूंगा").

Extend `FILLER_KEYWORDS` and `FILLER_PHRASES_BY_CATEGORY` with more languages
by adding entries under the same three category keys. Fallback is always
`neutral` if no keyword matches.
"""

import re
from typing import Dict, List


# --- Keyword lists ---------------------------------------------------------
# Each category's list is a flat mix of Devanagari + Latin (Hinglish + English).
# For Latin-script keywords, matching uses word-boundary regex to avoid false
# positives (e.g. "no" inside "know"). For non-Latin, substring match is used.

FILLER_KEYWORDS: Dict[str, List[str]] = {
    "negative": [
        # Hindi (Devanagari) — refusals, inability, complaints
        "नहीं", "नही", "ना ", " ना", "मत ", "नामुमकिन", "मुश्किल", "कठिन",
        "पैसे नहीं", "पैसा नहीं", "पैसे नही", "पैसा नही",
        "नहीं है", "नहीं दे", "नहीं कर", "नहीं होगा", "नहीं हो",
        "नहीं पाऊंगा", "नहीं पाऊँगा", "नहीं दे पाऊंगा", "नहीं कर पाऊंगा",
        "नहीं मिल", "नहीं मिला", "नहीं आया", "नहीं आयी", "नहीं आये",
        "नौकरी नहीं", "जॉब नहीं", "काम नहीं", "इनकम नहीं", "सैलरी नहीं",
        "बेरोजगार", "गरीब", "तंगी", "आर्थिक तंगी", "कर्ज़",
        "समस्या", "प्रॉब्लम", "प्रोब्लम", "दिक्कत", "परेशानी", "मुसीबत",
        "कम है", "कम पड़", "पूरा नहीं", "पूरे नहीं", "अभी नहीं",
        "कैसे दूं", "कैसे दूँ", "कहाँ से लाऊं", "कहाँ से लाऊँ",
        "मना", "मनाही", "इनकार",
        # Hinglish Latin-script
        "nahi", "nahin", "nai", "naa", "mat",
        "paise nahi", "paisa nahi",
        # English
        "no", "not", "cant", "can not", "cannot",
        "wont", "wouldnt", "shouldnt", "couldnt",
        "unable", "impossible", "difficult", "hard", "tough",
        "problem", "issue", "trouble", "sorry",
        "broke", "poor", "jobless", "unemployed",
        "no money", "no job", "no income",
        "never",
    ],
    "positive": [
        # Hindi (Devanagari) — agreement, commitment
        "हाँ", "हां", "जी हाँ", "जी हां", "जी बिल्कुल",
        "ठीक है", "ठीक हैं", "अच्छा", "अच्छा है", "बिल्कुल", "सही",
        "पक्का", "जरूर", "जरूरी", "सहमत", "मान गया", "मंजूर",
        "कर दूंगा", "कर दूँगा", "करूंगा", "करूँगा",
        "दे दूंगा", "दे दूँगा", "दूंगा", "दूँगा",
        "पेमेंट कर दूं", "पेमेंट कर दूँ", "पे कर दूंगा", "पे कर दूँगा",
        "हो जाएगा", "बन जाएगा", "मिल जाएगा",
        "जरूर करूंगा", "जरूर करूँगा", "कोशिश करूंगा", "कोशिश करूँगा",
        "समझ गया", "समझ गयी", "समझ आया",
        # Hinglish
        "haan", "han", "ji haan", "ji han", "ok ji", "okay ji",
        "theek hai", "thik hai",
        # English
        "yes", "yeah", "yep", "yup", "yah",
        "sure", "ok", "okay", "alright", "fine", "great",
        "agreed", "definitely", "absolutely", "certainly",
        "sounds good", "of course",
        "will do", "i can", "i will",
        "no problem", "will pay", "can pay",
    ],
}


# --- Filler phrases per category ------------------------------------------

FILLER_PHRASES_BY_CATEGORY: Dict[str, List[str]] = {
    "positive": ["अच्छा जी", "हाँ हाँ", "ठीक है", "बहुत बढ़िया", "समझ गया"],
    "negative": ["हम्म", "देखिए", "अच्छा", "समझा", "ओहो"],
    "neutral":  ["मतलब", "एक सेकंड", "ठीक", "जी", "अच्छा"],
}


# Flat list used while categorization is deferred. Longer, meaning-neutral
# Hindi acknowledgments that Murf's Hindi voice pronounces cleanly. Every
# phrase here is a pure "I hear you, hold on" filler — nothing that commits
# to agreement, disagreement, comprehension, or invites the user to continue.
# This makes them safe to play against ANY user turn, since we don't yet know
# what the LLM's actual reply will be.
SIMPLE_FILLERS: List[str] = [
    "जी सुन रहा हूँ",
    "एक मिनट रुकिए",
    "मैं देख रहा हूँ",
    "थोड़ा रुकिए जी",
    "हम्म देखते हैं",
    "जी एक सेकंड",
]


# --- Matching --------------------------------------------------------------

def _matches_any(text_lower: str, keywords: List[str]) -> bool:
    for kw in keywords:
        kw_lower = kw.lower()
        if not kw_lower:
            continue
        if all(ord(c) < 128 for c in kw_lower):
            # Latin: word-boundary match
            if re.search(rf"\b{re.escape(kw_lower)}\b", text_lower):
                return True
        else:
            # Devanagari / non-Latin: substring is fine
            if kw_lower in text_lower:
                return True
    return False


def classify_utterance(text: str) -> str:
    """Return `positive`, `negative`, or `neutral` (fallback).

    Negative is checked before positive because refusals dominate the sentiment
    of an utterance even when they contain positive-sounding tokens.
    """
    if not text:
        return "neutral"
    lowered = text.lower()
    if _matches_any(lowered, FILLER_KEYWORDS.get("negative", [])):
        return "negative"
    if _matches_any(lowered, FILLER_KEYWORDS.get("positive", [])):
        return "positive"
    return "neutral"


def pick_filler(category: str, last_index_by_category: Dict[str, int],
                phrases_by_category: Dict[str, List[str]],
                suffix: str = "...") -> str:
    """Round-robin pick from the category's phrase list, appending pause suffix."""
    phrases = phrases_by_category.get(category) or phrases_by_category.get("neutral") or []
    if not phrases:
        return ""
    idx = (last_index_by_category.get(category, -1) + 1) % len(phrases)
    last_index_by_category[category] = idx
    phrase = phrases[idx].rstrip()
    return phrase if phrase.endswith(suffix) else f"{phrase}{suffix}"
