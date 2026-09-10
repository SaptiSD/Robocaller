"""Compliance guardrails.

Automated pre-recorded and AI-voice calls to US consumers sit under the TCPA,
which carries statutory damages of $500-$1,500 *per call*. The FCC's February
2024 declaratory ruling put AI-generated voices explicitly in scope. This module
holds the mechanical parts of that: number normalization, per-recipient calling
windows derived from the area code, and the identification / opt-out language
that has to be in the script itself.

It does not make you compliant. Written consent records, National DNC registry
scrubbing, and state mini-TCPA rules are still on you. It stops the prototype
from doing the obviously illegal thing by accident.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

# --- phone numbers ----------------------------------------------------------

_DIGITS = re.compile(r"\D+")


def normalize(raw: str) -> str:
    """Best-effort E.164. Returns "" if it can't be made into a plausible number."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    has_plus = raw.startswith("+")
    digits = _DIGITS.sub("", raw)
    if not digits:
        return ""
    if has_plus:
        return "+" + digits if 8 <= len(digits) <= 15 else ""
    if len(digits) == 10:  # bare NANP
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""


def area_code(e164: str) -> str:
    return e164[2:5] if e164.startswith("+1") and len(e164) == 12 else ""


def pretty(e164: str) -> str:
    if e164.startswith("+1") and len(e164) == 12:
        return f"({e164[2:5]}) {e164[5:8]}-{e164[8:]}"
    return e164


# --- area code -> timezone --------------------------------------------------
# Assignments follow the dominant timezone of each NANP area code. Split codes
# (850, 906, 219, 807, 308 ...) are pinned to one side; that is imprecise by up
# to an hour, which is why the default calling window is tighter than the legal
# 8am-9pm.

_ZONE_CODES: dict[str, str] = {
    "America/New_York": (
        "203 475 860 959 "                                          # CT
        "302 "                                                      # DE
        "202 "                                                      # DC
        "239 305 321 352 386 407 448 561 656 689 727 754 772 786 "  # FL (peninsula)
        "813 863 904 941 954 "
        "229 404 470 478 678 706 762 770 912 943 "                  # GA
        "260 317 463 574 765 812 930 "                              # IN (east)
        "502 606 859 "                                              # KY (east)
        "207 "                                                      # ME
        "227 240 301 410 443 667 "                                  # MD
        "339 351 413 508 617 774 781 857 978 "                      # MA
        "231 248 269 313 517 586 616 679 734 810 947 989 "          # MI (lower)
        "603 "                                                      # NH
        "201 551 609 640 732 848 856 862 908 973 "                  # NJ
        "212 315 329 332 347 363 516 518 585 607 631 646 680 716 "  # NY
        "718 838 845 914 917 929 934 "
        "252 336 472 704 743 828 910 919 980 984 "                  # NC
        "216 220 234 326 330 380 419 440 513 567 614 740 937 "      # OH
        "215 223 267 272 412 445 484 570 582 610 717 724 814 835 "  # PA
        "878 "
        "401 "                                                      # RI
        "803 839 843 854 864 "                                      # SC
        "423 865 "                                                  # TN (east)
        "802 "                                                      # VT
        "276 434 540 571 703 757 804 826 948 "                      # VA
        "304 681 "                                                  # WV
        "226 249 289 343 365 367 382 416 418 437 438 450 468 514 "  # ON / QC
        "519 548 579 581 613 647 683 705 742 753 819 873 905 942"
    ),
    "America/Chicago": (
        "205 251 256 334 659 938 "                                  # AL
        "327 479 501 870 "                                          # AR
        "850 "                                                      # FL (panhandle)
        "217 224 309 312 331 447 464 618 630 708 730 773 779 815 "  # IL
        "847 861 872 "
        "219 "                                                      # IN (northwest)
        "319 515 563 641 712 "                                      # IA
        "316 620 785 913 "                                          # KS
        "270 364 "                                                  # KY (west)
        "225 318 337 504 985 "                                      # LA
        "906 "                                                      # MI (upper)
        "218 320 507 612 651 763 952 "                              # MN
        "228 601 662 769 "                                          # MS
        "235 314 417 557 573 636 660 816 "                          # MO
        "308 402 531 "                                              # NE
        "701 "                                                      # ND
        "405 539 572 580 918 "                                      # OK
        "605 "                                                      # SD
        "615 629 731 901 931 "                                      # TN (west)
        "210 214 254 281 325 346 361 409 430 469 512 682 713 726 "  # TX
        "737 806 817 830 832 903 936 940 945 956 972 979 "
        "262 274 414 534 608 715 920 "                              # WI
        "204 431 584 "                                              # MB
        "807"                                                       # ON (northwest)
    ),
    "America/Denver": (
        "303 719 720 970 983 "                                      # CO
        "208 986 "                                                  # ID
        "406 "                                                      # MT
        "505 575 "                                                  # NM
        "915 "                                                      # TX (El Paso)
        "385 435 801 "                                              # UT
        "307"                                                       # WY
    ),
    "America/Los_Angeles": (
        "209 213 279 310 323 341 350 357 369 408 415 424 442 510 "  # CA
        "530 559 562 619 626 628 650 657 661 669 707 714 738 747 "
        "760 764 805 818 820 831 837 840 858 909 916 925 935 949 951 "
        "702 725 775 "                                              # NV
        "458 503 541 971 "                                          # OR
        "206 253 360 425 509 564 "                                  # WA
        "236 250 257 604 672 778"                                   # BC
    ),
    "America/Phoenix": "480 520 602 623 928",                       # AZ (no DST)
    "America/Edmonton": "368 403 587 780 825",                      # AB
    "America/Regina": "306 474 639",                                # SK (no DST)
    "America/Halifax": "506 782 902",                               # NB / NS / PE
    "America/St_Johns": "709",                                      # NL
    "America/Anchorage": "907",                                     # AK
    "Pacific/Honolulu": "808",                                      # HI (no DST)
    "America/Puerto_Rico": "340 787 939",                           # PR / USVI
    "Pacific/Guam": "671",
}

AREA_CODE_TZ: dict[str, str] = {}
for _zone, _codes in _ZONE_CODES.items():
    for _code in _codes.split():
        if _code in AREA_CODE_TZ:  # a split code assigned twice is a data bug
            raise ValueError(
                f"area code {_code} is in both {AREA_CODE_TZ[_code]} and {_zone}"
            )
        AREA_CODE_TZ[_code] = _zone

# Toll-free and other non-geographic prefixes carry no location signal.
NON_GEOGRAPHIC = {"800", "833", "844", "855", "866", "877", "888", "900"}

# When the area code tells us nothing, require the local time to be inside the
# window in every mainland US zone before dialing.
_FALLBACK_ZONES = ("America/New_York", "America/Los_Angeles")


def timezone_for(e164: str) -> str | None:
    code = area_code(e164)
    if not code or code in NON_GEOGRAPHIC:
        return None
    return AREA_CODE_TZ.get(code)


# --- calling windows --------------------------------------------------------

def _parse_hhmm(text: str, fallback: time) -> time:
    try:
        hh, mm = str(text).split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return fallback


def window_check(
    e164: str,
    when_utc: datetime,
    start: str = "09:00",
    end: str = "20:00",
) -> tuple[bool, datetime | None, str]:
    """Is `when_utc` inside the recipient's local calling window?

    Returns (allowed, next_open_utc, zone_label). When not allowed,
    `next_open_utc` is the next moment the window opens for this recipient.
    """
    start_t = _parse_hhmm(start, time(9, 0))
    end_t = _parse_hhmm(end, time(20, 0))

    zone = timezone_for(e164)
    zones = [zone] if zone else list(_FALLBACK_ZONES)
    label = zone or "unknown (conservative)"

    opens: list[datetime] = []
    for name in zones:
        tz = ZoneInfo(name)
        local = when_utc.astimezone(tz)
        if start_t <= local.time() < end_t:
            continue  # this zone is happy
        # Find the next opening in this zone.
        candidate = local.replace(
            hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0
        )
        if candidate <= local:
            candidate += timedelta(days=1)
        opens.append(candidate.astimezone(when_utc.tzinfo))

    if not opens:
        return True, None, label
    return False, max(opens), label


# --- script hygiene ---------------------------------------------------------

MAX_MESSAGE_CHARS = 3000  # Twilio caps an inline TwiML payload at 4000 chars.


def build_script(
    message: str,
    business_name: str = "",
    callback_number: str = "",
    add_identification: bool = True,
    add_opt_out: bool = True,
) -> str:
    """Wrap the campaign message in the language the FCC requires.

    A pre-recorded telemarketing call must identify the caller at the start and
    give a way to opt out. We prepend/append rather than trusting the operator to
    remember, and skip a piece if the message already covers it.
    """
    body = (message or "").strip()
    parts: list[str] = []
    lower = body.lower()

    if add_identification and business_name:
        if business_name.lower() not in lower:
            parts.append(f"Hello. This is an automated message from {business_name}.")

    parts.append(body)

    if add_opt_out:
        if callback_number:
            parts.append(
                "To be removed from this calling list, please call "
                f"{spell_number(callback_number)}."
            )
        else:
            parts.append(
                "To be removed from this calling list, please contact us at the "
                "number shown on your caller ID."
            )

    return " ".join(p for p in parts if p).strip()


def spell_number(raw: str) -> str:
    """Render a phone number so text-to-speech reads it as digits, not a quantity."""
    e164 = normalize(raw) or raw
    digits = _DIGITS.sub("", e164)
    if e164.startswith("+1") and len(digits) == 11:
        digits = digits[1:]
    if not digits:
        return raw
    grouped = [digits[i:i + 3] for i in range(0, len(digits), 3)]
    return ", ".join(" ".join(g) for g in grouped)


def estimate_seconds(script: str) -> int:
    """Rough spoken duration. TTS runs about 150 words per minute."""
    words = len((script or "").split())
    return max(1, round(words / 150 * 60))


def validate_script(script: str) -> list[str]:
    problems: list[str] = []
    if not script.strip():
        problems.append("The message is empty.")
    if len(script) > MAX_MESSAGE_CHARS:
        problems.append(
            f"The message is {len(script)} characters; the limit is {MAX_MESSAGE_CHARS}."
        )
    if estimate_seconds(script) > 90:
        problems.append(
            "The message runs over 90 seconds. Most recipients hang up well before that."
        )
    return problems
