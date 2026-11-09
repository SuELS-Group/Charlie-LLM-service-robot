"""Bounded clarification matching independent of best-effort speaker attribution."""
import json
import math
import re

from .transcription import parse_user_message


def speaker_from_text(text):
    match = re.match(r'^\s*\[User\s+([^\]]+)\]\s*:', text, re.IGNORECASE)
    return ' '.join(match.group(1).split()).casefold() if match else None


def interaction_message(raw):
    text, language = parse_user_message(raw)
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    return text, language, speaker_from_text(text), payload.get('clarification_id'), payload.get('new_request') is True


def monitor_message(raw):
    """Return battery-event text, keeping ASR/UI text out of legacy routing."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        if payload.get('source') == 'battery_monitor':
            return payload.get('text') if isinstance(payload.get('text'), str) else ''
        return None
    if isinstance(raw, str) and (raw.startswith('Battery status ') or raw == 'Battery critical.'):
        return raw
    return None


def addressed_text(text):
    text = re.sub(r'^\s*\[User\s+[^\]]+\]\s*:\s*', '', text, flags=re.IGNORECASE)
    # ASR commonly retains greetings and repeated wake words. Strip only the
    # address at the beginning, preserving the actual request/answer.
    text = re.sub(r'^\s*(?:(?:hey|hi|hallo|hello|ok|okay)[\s,:.!-]+)?'
                  r'(?:charlie\b[\s,:.!-]*)+', '', text, flags=re.IGNORECASE)
    return ' '.join(re.findall(r'\w+', text.casefold()))


def is_bare_confirmation(text):
    return addressed_text(text) in {'ja', 'nein', 'yes', 'no', 'ok', 'okay', 'ja bitte',
        'nein danke', 'yes please', 'no thanks', 'yes thank you'}


def _gripper_clauses(text):
    """Keep question marks on their clause, excluding a bare wake-word clause."""
    if not isinstance(text, str):
        return []
    body = re.sub(r'^\s*\[User\s+[^\]]+\]\s*:\s*', '', text, flags=re.IGNORECASE)
    body = re.sub(r"\byou['’]re\b", 'you are', body, flags=re.IGNORECASE)
    body = re.sub(r"\baren['’]t\b", 'are not', body, flags=re.IGNORECASE)
    if any(character in body for character in '"\'„“”‘’«»'):
        return []
    clauses = []
    for part, punctuation in re.findall(r'([^.!?;\n]+)([.!?;\n]*)', body):
        value = re.sub(r'(?:\s+charlie)+$', '', addressed_text(part))
        if not value:  # "Charlie?" is an address, not a possession question.
            continue
        value = re.sub(r'\b(?:jetzt|gerade|aktuell|bereits|schon|noch|mehr|'
                       r'currently|now|still|already|anymore)\b\s*', '', value).strip()
        clauses.append((value, '?' in punctuation))
    return clauses


_POSSESSION_UPDATE = (r'(?:(?:bitte )?(?:aktualisiere|korrigiere) deinen '
                      r'(?:haltestatus|greiferzustand|status)|'
                      r'(?:please )?(?:update|correct) your (?:holding|gripper) (?:status|state))')


def gripper_possession_report(text):
    """Recognize explicit human corrections, including repeated spoken reports.

    Every clause must be a consistent assertion or a state-update instruction.
    Questions, quotations, uncertainty and physical action requests never write
    possession. A question mark attached only to the wake word is harmless.
    """
    states = set()
    for value, question in _gripper_clauses(text):
        if question:
            return None
        value = re.sub(r'^(?:(?:doch|nein|ja|no|yes|actually|korrektur|correction|charlie)\s+)+', '', value)
        value = re.sub(r'^(?:(?:und )?ich sage dir(?: dass)?|i am telling you(?: that)?) ', '', value)
        if value in {'das ist ein befehl', 'this is an instruction'} or re.fullmatch(_POSSESSION_UPDATE, value):
            continue
        value = re.sub(rf' (?:und |and )?{_POSSESSION_UPDATE}$', '', value)
        state = _gripper_assertion(value)
        if state is None:
            return None
        states.add(state)
    return next(iter(states)) if len(states) == 1 else None


def _gripper_assertion(value):
    """Match a complete state clause, never a substring inside another request."""
    value = re.sub(r'\bdoch\b\s*', '', value)
    # A standalone status correction may also describe the arm. Only consume
    # these explicit assertions; action requests and uncertain claims still do
    # not become writes or evidence that a controller has stopped.
    value = re.sub(r'^(?:dein arm ist (?:in einem guten zustand|in ordnung|okay|ok)|'
                   r'your arm is (?:in good condition|okay|ok)) (?:und|and) ', '', value)
    location = r'(?:in (?:der|deiner) hand|im greifer|in deinem greifer)'
    if re.fullmatch(rf'du (?:hast|hältst) (?:(?:gar |überhaupt )?nichts|'
                    rf'kein(?:en|e)? (?:item|gegenstand|objekt))(?: {location})?', value):
        return 'empty'
    if value in {'du hast nichts in der hand', 'du hast nichts in deiner hand',
        'du hast keinen gegenstand in der hand', 'du hast keinen gegenstand in deiner hand',
        'du hast kein item in der hand', 'du hast kein item in deiner hand',
        'du hast kein item', 'du hast keinen gegenstand',
        'du hältst nichts', 'du hältst keinen gegenstand', 'du hältst kein item',
        'dein greifer ist leer', 'deine hand ist leer',
        'your gripper is empty', 'your hand is empty', 'you are not holding anything',
        'you are holding nothing', 'you have nothing in your hand'}:
        return 'empty'
    if value in {'dein greifer ist nicht leer', 'your gripper is not empty'}:
        return 'holding_assumed'
    if re.search(r'\b(?:nicht|kein\w*|vielleicht|eventuell|wenn|falls|aber|und|oder|'
                 r'not|no|maybe|might|possibly|if|but|and|or)\b', value):
        return None
    if value in {'du hältst etwas', 'du hältst einen gegenstand', 'du hältst ein item',
        'du hast einen gegenstand', 'du hast ein item',
        'du hältst den gegenstand', 'du hältst das item', 'dein greifer ist belegt',
        'you are holding something', 'you are holding an item', 'you hold an item', 'you have an item',
        'your gripper is occupied'}:
        return 'holding_assumed'
    article = r'(?:ein(?:en|e)?|dein(?:en|e)?|den|die|das|a|an|the|your|some)'
    # Without a hand/gripper location, restrict this to object terms; "du hältst
    # dein Wort/einen Vortrag" must not be interpreted as a held object.
    if re.fullmatch(rf'du (?:hast|hältst) {article} (?:item|gegenstand|objekt|ball|tasse|becher|flasche)', value):
        return 'holding_assumed'
    noun = rf'{article} \w+(?: \w+){{0,3}}'
    if re.fullmatch(rf'du (?:hast|hältst) (?:etwas|{noun}) {location}', value):
        return 'holding_assumed'
    if re.fullmatch(rf'you (?:have|are holding) (?:something|{noun}) in your (?:hand|gripper)', value):
        return 'holding_assumed'
    return None


def empty_gripper_report(text):
    """Compatibility predicate for callers interested only in empty reports."""
    return gripper_possession_report(text) == 'empty'


def gripper_possession_query(text):
    """Recognize standalone status questions for a fresh, read-only lookup."""
    clauses = _gripper_clauses(text)
    if len(clauses) != 1:
        return False
    value, question = clauses[0]
    if question and _gripper_assertion(value) is not None:
        return True  # Declarative word order with interrogative punctuation.
    for prefix, statement in [('hältst du ', 'du hältst '), ('hast du ', 'du hast '),
            ('ist dein greifer ', 'dein greifer ist '), ('ist deine hand ', 'deine hand ist '),
            ('are you ', 'you are '), ('do you have ', 'you have '),
            ('is your gripper ', 'your gripper is '), ('is your hand ', 'your hand is ')]:
        if value.startswith(prefix) and _gripper_assertion(statement + value[len(prefix):]) is not None:
            return True
    return value in {'hast du etwas in der hand', 'hast du etwas in deiner hand',
        'hast du einen gegenstand in der hand', 'hast du ein item in der hand',
        'hältst du etwas', 'hältst du einen gegenstand', 'hältst du ein item',
        'was hältst du in der hand', 'was hast du in der hand',
        'ist dein greifer leer', 'ist deine hand leer', 'ist dein greifer belegt',
        'wie ist dein greiferzustand', 'was ist dein greiferzustand',
        'are you holding anything', 'are you holding an item', 'are you holding something',
        'do you have anything in your hand', 'what are you holding',
        'what are you holding in your hand', 'is your gripper empty', 'is your hand empty'}


def requests_new_task(text):
    value = addressed_text(text)
    return any(value.startswith(prefix) for prefix in ('new request ', 'new task ', 'neuer auftrag ', 'neue aufgabe '))


def handoff_target_person(person, user_request, replies=()):
    """Keep spoken names; speaker attribution alone does not select a recipient."""
    if not isinstance(person,str) or not person.strip():
        return person
    name = ' '.join(person.split())
    texts = [user_request, *replies]
    body = ' '.join(re.sub(r'^\s*\[User\s+[^\]]+\]\s*:\s*','',text,flags=re.IGNORECASE)
        for text in texts if isinstance(text,str))
    pattern = re.escape(name).replace(r'\ ',r'\s+')
    if re.search(rf'(?<!\w){pattern}(?:s|[’\']s)?(?!\w)',body,flags=re.IGNORECASE):
        return name
    speaker = speaker_from_text(user_request)
    if speaker and name.casefold() == speaker:
        return None
    raise ValueError('The handover recipient name was not spoken in the current request. '
        'Use the explicitly named person, or omit person to select the nearest visible person. '
        'Never obtain a handover recipient from the speaker prefix or old conversation history.')


def matching_voice_window(window, wake_word_detected, issued_at, now):
    if not isinstance(window, dict) or not window.get('request_id'):
        return False
    expires_at, opened_at = window.get('expires_at'), window.get('opened_at')
    if not all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in (expires_at, opened_at, issued_at, now)):
        return False
    if now >= expires_at or issued_at < opened_at or issued_at > expires_at:
        return False
    # Anyone may answer the currently open question. Speaker recognition is
    # metadata, not a reliable ownership/authentication mechanism. Require the
    # wake word for everyone unless a supervised single-user mode opts out.
    return bool(wake_word_detected or window.get('allow_without_wake_word') is True)
