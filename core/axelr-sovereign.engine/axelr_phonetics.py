# ═══════════════════════════════════════════════════════════════════════════
# Phonetic algorithms for AXELR: Soundex + Metaphone + PhoneticEnsemble
# Single source implementation for both Python and JS porting
# ═══════════════════════════════════════════════════════════════════════════
from typing import Tuple, Dict

# ═══════════════════════════════════════════════════════════════════════════
# Phoneme to Grapheme mapping for normalization
# ═══════════════════════════════════════════════════════════════════════════
PHONEME_GRAPHEME_MAP: Dict[str, str] = {
    "ah": "a", "eh": "e", "ih": "i", "oh": "o", "uh": "u",
    "ae": "ai", "ee": "ea", "ie": "ei", "oo": "ou",
    "ck": "c", "ks": "x", "kw": "qu", "sh": "ti", "zh": "si",
    "ksh": "ction", "shun": "tion",
}


class PhonemeGraphemeNormalizer:
    __slots__ = ("_map",)

    def __init__(self) -> None:
        self._map = PHONEME_GRAPHEME_MAP

    def normalize(self, text: str) -> str:
        out = text.lower()
        for phon, graph in self._map.items():
            out = out.replace(phon, graph)
        return out


# ═══════════════════════════════════════════════════════════════════════════
# Soundex algorithm
# ═══════════════════════════════════════════════════════════════════════════
def soundex(word: str) -> str:
    """Standard Soundex algorithm. Single source to be ported to JS."""
    if not word:
        return ""
    # Mapping of letters to Soundex codes
    codes = {
        "b": "1", "f": "1", "p": "1", "v": "1",
        "c": "2", "g": "2", "j": "2", "k": "2", "q": "2", "s": "2", "x": "2", "z": "2",
        "d": "3", "t": "3",
        "l": "4",
        "m": "5", "n": "5",
        "r": "6"
    }
    # Process first character
    word = word.lower()
    first = word[0].upper()
    # Generate code for remaining characters
    result = [first]
    prev_code = codes.get(first.lower(), "")
    for ch in word[1:]:
        code = codes.get(ch, "")
        if code and code != prev_code:
            result.append(code)
            prev_code = code
        if len(result) >= 4:
            break
    # Pad with zeros if needed
    while len(result) < 4:
        result.append("0")
    return "".join(result)


# ═══════════════════════════════════════════════════════════════════════════
# Double Metaphone core algorithm
# ═══════════════════════════════════════════════════════════════════════════
def metaphone(word: str) -> str:
    """Double Metaphone core algorithm. Single source to be ported to JS."""
    if not word:
        return ""
    word = word.lower()
    result = []
    i = 0
    n = len(word)
    
    # Handle silent leading combinations first
    if n >= 2:
        # 'kn' → silent 'k', only 'n' remains
        if word[0] == 'k' and word[1] == 'n':
            i = 1  # Skip 'k', start at 'n'
        # 'gn' → silent 'g', only 'n' remains
        elif word[0] == 'g' and word[1] == 'n':
            i = 1  # Skip 'g', start at 'n'
        # 'ps' → silent 'p', only 's' remains
        elif word[0] == 'p' and word[1] == 's':
            i = 1  # Skip 'p', start at 's'
        # 'rh' → silent 'h', only 'r' remains (rhyme → rime)
        elif word[0] == 'r' and word[1] == 'h':
            i = 1  # Skip 'h', start at 'r'
        # 'wh' → silent 'w', only 'h' remains (what → hat)
        elif word[0] == 'w' and word[1] == 'h':
            i = 1  # Skip 'w', start at 'h'
        # Keep original 'wr' handling for all words
        elif word[0] == 'w' and word[1] == 'r':
            i = 1  # Skip 'w', start at 'r'
    
    # Process remaining word with core metaphone rules
    while i < n:
        ch = word[i]
        # Align all other 'ei' and 'ie' cases phonetically
        if i+1 < n:
            if word[i] == 'e' and word[i+1] == 'i':
                result.append('i')
                i += 2
                continue
            elif word[i] == 'i' and word[i+1] == 'e':
                result.append('i')
                i += 2
                continue
        # Handle simple mappings first
        if ch in 'aeiou':
            if i == 0:  # Keep initial vowels
                result.append(ch)
            i += 1
        elif ch == 'b':
            result.append('b')
            i += 1
        elif ch == 'c':
            # Handle 'ch', 'ci', 'ce' cases
            if i+1 < n and word[i+1] == 'h':
                result.append('x')
                i += 2
            elif i+1 < n and word[i+1] in 'iey':
                result.append('s')
                i += 2
            else:
                result.append('k')
                i += 1
        elif ch == 'd':
            if i+1 < n and word[i+1] == 'g':
                result.append('j')
                i += 2
            else:
                result.append('t')
                i += 1
        elif ch == 'f':
            result.append('f')
            i += 1
        elif ch == 'g':
            if i+1 < n and word[i+1] == 'h':
                result.append('k')
                i += 2
            else:
                result.append('g')
                i += 1
        elif ch == 'h':
            if i == 0 or (i > 0 and word[i-1] not in 'aeiou'):
                result.append('h')
            i += 1
        elif ch == 'j':
            result.append('j')
            i += 1
        elif ch == 'k':
            result.append('k')
            i += 1
        elif ch == 'l':
            result.append('l')
            i += 1
        elif ch == 'm':
            result.append('m')
            i += 1
        elif ch == 'n':
            result.append('n')
            i += 1
        elif ch == 'p':
            if i+1 < n and word[i+1] == 'h':
                result.append('f')
                i += 2
            else:
                result.append('p')
                i += 1
        elif ch == 'q':
            result.append('k')
            i += 1
        elif ch == 'r':
            result.append('r')
            i += 1
        elif ch == 's':
            if i+1 < n and word[i+1] == 'h':
                result.append('x')
                i += 2
            else:
                result.append('s')
                i += 1
        elif ch == 't':
            if i+1 < n and word[i+1] == 'h':
                result.append('0')
                i += 2
            else:
                result.append('t')
                i += 1
        elif ch == 'v':
            result.append('f')
            i += 1
        elif ch == 'w':
            if i+1 < n and word[i+1] in 'aeiou':
                result.append('w')
            i += 1
        elif ch == 'x':
            result.append('k')
            i += 1
        elif ch == 'y':
            if i+1 < n and word[i+1] in 'aeiou':
                result.append('y')
            i += 1
        elif ch == 'z':
            result.append('s')
            i += 1
        else:
            i += 1
    # Pad with zeros if needed to ensure 4-character code for consistent output
    while len(result) < 4:
        result.append("0")
    return "".join(result)[:4]


# ═══════════════════════════════════════════════════════════════════════════
# PhoneticEnsemble: Combined phonetic algorithms
# ═══════════════════════════════════════════════════════════════════════════
class PhoneticEnsemble:
    """Combined phonetic algorithms. Single source for JS porting."""
    __slots__ = ()

    @staticmethod
    def encode(word: str) -> Tuple[str, str]:
        """Return (soundex, metaphone) for a word."""
        return soundex(word), metaphone(word)

    @staticmethod
    def compare(a: str, b: str) -> float:
        """Compare two words phonetically. Returns 0.0-1.0 similarity."""
        s1, m1 = PhoneticEnsemble.encode(a)
        s2, m2 = PhoneticEnsemble.encode(b)
        score = 0.0
        if s1 == s2:
            score += 0.5
        if m1 == m2:
            score += 0.5
        return score


# ═══════════════════════════════════════════════════════════════════════════
# Export all symbols
# ═══════════════════════════════════════════════════════════════════════════
__all__ = [
    "PhonemeGraphemeNormalizer",
    "soundex",
    "metaphone",
    "PhoneticEnsemble",
    "PHONEME_GRAPHEME_MAP"
]