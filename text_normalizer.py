import re
from loguru import logger

# Try to import number to words libraries
try:
    from indic_numtowords import num2words as indic_num2words
except ImportError:
    indic_num2words = None

try:
    from num2words import num2words as english_num2words
except ImportError:
    english_num2words = None

CURRENCY_WORDS = {
    "hi": "रुपये",
    "mr": "रुपये",
    "gu": "રૂપિયા",
    "kn": "ರೂಪಾಯಿ",
    "te": "రూపాయలు",
    "ta": "ரூபாய்",
    "bn": "টাকা",
    "pa": "ਰੁਪਏ",
    "en": "rupees"
}

def num_to_words(num: int, lang_code: str) -> str:
    """Convert a number to words in the specified language."""
    # Normalize language code to two letters
    base_lang = lang_code.split("-")[0].lower() if lang_code else "en"
    
    if base_lang == "en":
        if english_num2words:
            try:
                return english_num2words(num, lang='en_IN')
            except Exception as e:
                logger.warning(f"num2words en_IN failed: {e}")
        return str(num)
    
    # Regional Indian languages
    if base_lang in ["hi", "mr", "gu", "kn", "te", "ta", "bn", "pa"]:
        if indic_num2words:
            try:
                return indic_num2words(num, lang=base_lang)
            except Exception as e:
                logger.warning(f"indic_num2words {base_lang} failed: {e}")
        # Fallback to English words if indic library fails or is not available
        if english_num2words:
            try:
                return english_num2words(num, lang='en_IN')
            except Exception:
                pass
    return str(num)

def normalize_text(text: str, lang: str = "en") -> str:
    # Strip XML/HTML tags (e.g. <speech> or </speech>)
    text = re.sub(r'<[^>]+>', '', text)

    # Resolve base language code
    base_lang = lang.split("-")[0].lower() if lang else "en"
    if base_lang not in CURRENCY_WORDS:
        # Check if the text contains Devanagari (Hindi/Marathi/etc) characters as fallback
        has_hindi = any(ord(char) >= 0x0900 and ord(char) <= 0x097F for char in text)
        base_lang = "hi" if has_hindi else "en"

    # Helper to convert a numeric string to words
    def replace_num(match):
        num_str = match.group(1).replace(",", "")
        try:
            num = int(num_str)
            return num_to_words(num, base_lang)
        except ValueError:
            return match.group(0)

    # Replace currency pattern first, e.g., Rs. 2,000 or ₹ 20,000
    currency_regex = r'\b(Rs\.?|₹|rupees|रुपये)\s*(\d+(?:,\d+)*)\b|\b(\d+(?:,\d+)*)\s*(rupees|रुपये)\b'
    
    def currency_sub(m):
        if m.group(2):
            val = m.group(2).replace(",", "")
            try:
                num = int(val)
                currency_word = CURRENCY_WORDS.get(base_lang, "rupees")
                return f"{num_to_words(num, base_lang)} {currency_word}"
            except ValueError:
                pass
        elif m.group(3):
            val = m.group(3).replace(",", "")
            try:
                num = int(val)
                currency_word = CURRENCY_WORDS.get(base_lang, "rupees")
                return f"{num_to_words(num, base_lang)} {currency_word}"
            except ValueError:
                pass
        return m.group(0)

    text = re.sub(currency_regex, currency_sub, text, flags=re.IGNORECASE)

    # Now replace remaining numbers (e.g. 400 days, DPD, etc.)
    number_regex = r'\b(\d+(?:,\d+)*)\b'
    text = re.sub(number_regex, replace_num, text)
    
    return text
