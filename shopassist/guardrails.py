"""Deterministic guardrails around the LLM: regex/rule based, so they add ~0 ms and no API cost,
and every rule is unit-tested. They complement (not replace) the structural defences:
customer-scoped tools, policies in code, and two-step confirmation for actions."""
import re

# ---------------- input: secrets the LLM must never see ----------------
CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
CVV = re.compile(r"\b(cvv|cvc|security code)\b(\D{0,12})(\d{3,4})\b", re.I)
OTP_PIN = re.compile(r"\b(otp|one[- ]time password|upi pin|pin|password)\b(\D{0,12})(\d{4,8})\b", re.I)

# ---------------- logs: personal data masked before storage ----------------
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")

INJECTION = re.compile("|".join([
    r"ignore (?:all |any |your |the )?(?:previous|prior|above|earlier|system) (?:instructions|rules|prompts?)",
    r"disregard (?:all |your |the )?(?:previous|prior|above|system)",
    r"\b(?:system|developer|hidden) (?:prompt|instructions|message)\b",
    r"\byou are now\b",
    r"\b(?:admin|developer|god|debug|jailbreak|dan) mode\b",
    r"\bact as (?:an? )?(?:admin|administrator|developer|system)\b",
    r"\b(?:reveal|print|show|repeat) (?:me )?(?:your|the) (?:instructions|prompt|rules)\b",
    r"\bpretend (?:you are|to be)\b",
    r"\boverride\b.*\b(?:policy|rules|limit)",
]), re.I)
HUMAN_REQUEST = re.compile(
    r"\b(?:human|real person|live (?:agent|person|chat)|actual person|representative|supervisor|manager|"
    r"customer care executive|talk to (?:an? |some)?(?:one|agent|person|specialist))\b", re.I)
LEGAL_OR_SAFETY = re.compile(
    r"\b(?:lawyer|legal action|legal notice|sue|court|consumer forum|police|fir|fraud|scam|"
    r"fire|burn(?:ed|t)?|shock|injur(?:y|ed)|explod(?:e|ed))\b", re.I)

AFFIRMATIVE = re.compile(r"\b(?:yes|yeah|yep|yup|sure|ok(?:ay)?|confirm(?:ed)?|go ahead|please do|do it|proceed|"
                         r"correct|that'?s right|please (?:cancel|return)|sounds good)\b", re.I)
NEGATIVE = re.compile(r"\b(?:no|nope|don'?t|do not|wait|stop|hold on|not now|never ?mind|changed my mind)\b", re.I)

# ---------------- output ----------------
ORDER_REF = re.compile(r"(?:order\s*(?:id|number|no\.?)?\s*#?\s*|#)(\d{4,6})\b", re.I)
KB_CITATION = re.compile(r"\[(KB\d+)\]")
ACTION_CLAIMS = [
    re.compile(r"\bI(?:'ve| have| just)?\s+(?:now\s+|successfully\s+|gone ahead and\s+)?"
               r"(?:cancel(?:l)?ed|initiated|created|scheduled|processed|raised|submitted|escalated|refunded|started|set up|arranged|booked)\b", re.I),
    re.compile(r"\b(?:return|cancellation|ticket|refund|pickup)(?: request)? (?:has been|was|is now) "
               r"(?:successfully )?(?:created|initiated|raised|scheduled|submitted|placed|processed|approved)\b", re.I),
]


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Remove payment card numbers (Luhn-validated), CVVs, OTPs and PINs before the text reaches
    the LLM or any log. Luhn avoids false positives on order ids, phone numbers and tracking codes."""
    flags = []

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            flags.append("redacted_card_number")
            return "[REDACTED CARD NUMBER]"
        return m.group()

    text = CARD_CANDIDATE.sub(card, text)
    text, n_cvv = CVV.subn(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    text, n_otp = OTP_PIN.subn(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    if n_cvv or n_otp:
        flags.append("redacted_secret_code")
    return text, flags


def mask_pii(text: str) -> str:
    """For storage/logs: keep enough to debug (domain, last digits), hide the rest."""
    text = EMAIL.sub(lambda m: m.group()[0] + "***@" + m.group().split("@")[1], text)
    return PHONE.sub(lambda m: "******" + re.sub(r"\D", "", m.group())[-4:], text)


def detect_injection(text: str) -> bool:
    return bool(INJECTION.search(text))


def routing_hints(text: str) -> list[str]:
    """Deterministic escalation triggers layered on top of the model's own judgement."""
    hints = []
    if LEGAL_OR_SAFETY.search(text):
        hints.append("The customer mentions a legal, safety or fraud issue: escalate_to_human with priority "
                     "'urgent' (after collecting the order id if relevant) and tell them the expected response time.")
    elif HUMAN_REQUEST.search(text):
        hints.append("The customer asked for a human: call escalate_to_human now (no need to troubleshoot first).")
    return hints


def is_confirmation(text: str) -> bool:
    return bool(AFFIRMATIVE.search(text)) and not NEGATIVE.search(text)


def order_ids_mentioned(text: str) -> set[int]:
    return {int(x) for x in re.findall(r"\b(\d{4,6})\b", text)}


def leaked_order_ids(reply: str, allowed: set[int]) -> set[int]:
    """Order ids in the reply that are neither the customer's nor ones they typed themselves."""
    return {int(x) for x in ORDER_REF.findall(reply)} - allowed


def claims_unperformed_action(reply: str, action_succeeded: bool) -> bool:
    """Catches the classic agent failure: 'I've cancelled your order' when no tool call succeeded."""
    return not action_succeeded and any(p.search(reply) for p in ACTION_CLAIMS)


def cited_kb_ids(reply: str) -> list[str]:
    return list(dict.fromkeys(KB_CITATION.findall(reply)))
