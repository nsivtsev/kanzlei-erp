"""Select fixed answer text using signals in the current question."""

import re

_ENGLISH = frozenset(
    "what when where who which why how is are were did does the this about please "
    "tell show summarize email invoice total amount much".split()
)
_GERMAN = frozenset(
    "was wann wo wer welcher welche welches warum wie ist sind hat der die das den dem "
    "welchen über bitte steht rechnung betrag gesamtbetrag summe".split()
)
_RUSSIAN = frozenset(
    "что когда где кто какой какая какие почему как какова сколько это скажи "
    "письмо письме письма счёт счёта счет счета сумма сумму итог итого".split()
)
_WORD = re.compile(r"[^\W\d_]+")
_CYRILLIC = re.compile(r"[а-яё]+")


def question_language(question: str) -> str:
    words = set(_WORD.findall(question.casefold()))
    en = len(words & _ENGLISH)
    de = len(words & _GERMAN)
    ru = len(words & _RUSSIAN)
    cyrillic = sum(bool(_CYRILLIC.fullmatch(word)) for word in words)
    if cyrillic >= 2 and ((ru and ru >= max(en, de)) or cyrillic > max(en, de)):
        return "ru"
    return "en" if en >= 2 and en > de else "de"


_ABSTENTION = {
    "de": "Keine ausreichenden Belege gefunden. Versuchen Sie es mit einem Dokumentnamen, "
    "einer Nummer oder einem Zeitraum.",
    "en": "I found no supporting evidence. Try including a document name, number, or time period.",
    "ru": "Не найдено достаточных подтверждений. Укажите название документа, номер или период.",
}
_AMOUNT = {
    "de": "Der Betrag beträgt {}.",
    "en": "The amount is {}.",
    "ru": "Сумма составляет {}.",
}
_EMAIL = {
    "de": "In der E-Mail steht:",
    "en": "The email says:",
    "ru": "В письме сказано:",
}


def abstention(question: str) -> str:
    return _ABSTENTION[question_language(question)]


def invoice_answer(question: str, amount: str) -> str:
    return _AMOUNT[question_language(question)].format(amount)


def email_answer(question: str, excerpt: str) -> str:
    return f"{_EMAIL[question_language(question)]}\n\n{excerpt}"
