"""Expand ambiguous numeric notation before German or English synthesis."""
import re
import unicodedata

from num2words import num2words


def speech_sentences(text: str) -> list[str]:
    sentences = []
    for part in re.split(r'(?<=[.!?])\s+(?=[A-ZÄÖÜ])', text.strip()):
        if sentences and re.search(r'\b(?:Dr|Prof|Mr|Mrs|Ms)\.$', sentences[-1]):
            sentences[-1] += ' ' + part
        elif part:
            sentences.append(part)
    return sentences


def speech_chunks(text: str, target_chars: int = 80) -> list[str]:
    """Prepare long sentences at clause boundaries, without cutting word groups."""
    chunks = []
    for sentence in speech_sentences(text):
        if target_chars <= 0 or len(sentence) <= target_chars * 1.5:
            chunks.append(sentence)
            continue
        clauses = re.split(r'(?<=[,;:])\s+|\s+(?=(?:und|and)\s)', sentence)
        sentence_chunks, current = [], ''
        for clause in clauses:
            if len(current) >= 24 and len(current) + len(clause) + 1 > target_chars:
                sentence_chunks.append(current)
                current = ''
            current = f'{current} {clause}'.strip()
        if current:
            if sentence_chunks and len(current) < 24:
                sentence_chunks[-1] += ' ' + current
            else:
                sentence_chunks.append(current)
        chunks.extend(sentence_chunks)
    return chunks


def spoken_text(text: str, language: str) -> str:
    if language not in ('de', 'en'):
        raise ValueError('Speech text requires an explicit German or English language')
    text = unicodedata.normalize('NFKC', text)

    def number(value):
        return num2words(int(value), lang=language)

    def room(match):
        parts = []
        for char in match[2]:
            if char.isdigit():
                parts.append(number(char))
            elif char.isalpha():
                parts.append(char.upper())
            elif char == '-':
                if parts:
                    parts[-1] += ','
            elif char in '/.':
                parts.append({'de': {'/': 'Schrägstrich', '.': 'Punkt'},
                              'en': {'/': 'slash', '.': 'point'}}[language][char])
        return match[1] + ' ' + ' '.join(parts)

    text = re.sub(r'\b(Raum|room)\s+([A-Za-z]{0,3}[-/ ]?\d(?:[\w./-]*\w)?)',
                  room, text, flags=re.I)

    def clock(match):
        hours, minutes = int(match[1]), int(match[2])
        seconds = int(match[3]) if match[3] is not None else None
        if hours > 23 or minutes > 59 or seconds is not None and seconds > 59:
            return match[0]
        second = ''
        if seconds is not None:
            if language == 'de':
                second = ' und ' + ('eine Sekunde' if seconds == 1 else number(seconds) + ' Sekunden')
            else:
                second = ' and ' + number(seconds) + (' second' if seconds == 1 else ' seconds')
        if language == 'de':
            hour = 'ein' if hours == 1 else number(hours)
            return hour + ' Uhr' + (' ' + number(minutes) if minutes else '') + second
        if not minutes and hours in (0, 12):
            noon = 'midnight' if hours == 0 else 'noon'
            return second.removeprefix(' and ') + ' past ' + noon if second else noon
        period = 'in the morning' if hours < 12 else 'in the afternoon' if hours < 18 else 'in the evening'
        minute = ' oh ' + number(minutes) if 0 < minutes < 10 else ' ' + number(minutes) if minutes else ''
        if seconds is not None and not minutes:
            minute = " o'clock"
        return number(hours % 12 or 12) + minute + second + ' ' + period

    text = re.sub(r'\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b(?:\s*Uhr\b)?', clock, text)
    separator = ' Komma ' if language == 'de' else ' point '
    text = re.sub(r'\b(\d+)[.,](\d+)\b', lambda m: number(m[1]) + separator
                  + ' '.join(number(c) for c in m[2]), text)
    text = re.sub(r'\b\d+\b', lambda m: number(m[0]), text)
    return re.sub(r'\s+', ' ', text).strip()
