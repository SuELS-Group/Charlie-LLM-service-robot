"""Shared knowledge mode and bounded, bilingual uncertainty replies."""
import os


def local_knowledge_mode():
    mode = os.environ.get('ROBOT_KNOWLEDGE_MODE', 'local' if
        os.environ.get('ROBOT_OPERATION_MODE') == 'study' else 'development')
    if mode not in {'development', 'local'}:
        raise ValueError('ROBOT_KNOWLEDGE_MODE must be development or local')
    # The study profile cannot be weakened by a conflicting environment flag.
    return mode == 'local' or os.environ.get('ROBOT_OPERATION_MODE') == 'study'


MESSAGES = {
    'no_evidence': ('I found no information in my local documents that answers that question.',
                    'Dazu habe ich in meinen lokalen Unterlagen keine passende Antwort gefunden.'),
    'conflict': ('My documents give conflicting information. Please check with the International Office.',
                 'Meine Unterlagen enthalten widersprüchliche Angaben. Bitte frage im International Office nach.'),
    'unavailable': ('I cannot access my local information right now. Please try again later.',
                     'Ich kann gerade nicht auf meine lokalen Informationen zugreifen. Bitte versuche es später erneut.'),
    'outdated': ('My knowledge index no longer matches the current documents. It needs updating before I can answer.',
                  'Mein Wissensindex passt nicht mehr zu den aktuellen Unterlagen. Er muss vor einer Antwort aktualisiert werden.'),
    'unapproved': ('The local information has not yet been approved for this study. Please ask the International Office.',
                    'Die lokalen Informationen sind für diese Studie noch nicht freigegeben. Bitte frage im International Office nach.'),
    'unsupported': ('I could not find sufficient source support for an answer. Please ask the International Office.',
                     'Ich konnte keine ausreichend belegte Antwort ermitteln. Bitte frage im International Office nach.'),
}


def knowledge_message(reason, language='en'):
    return MESSAGES.get(reason, MESSAGES['unavailable'])[language == 'de']
