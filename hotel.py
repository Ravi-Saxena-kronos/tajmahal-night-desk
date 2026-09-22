"""Tajmahal Hotel mock inventory. Tools never invent a rate or confirmation."""

from __future__ import annotations

import html
import json
import os
import random
import re
import smtplib
import threading
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional

def _data_dir() -> Path:
    override = os.environ.get("DATA_DIR")
    if override:
        return Path(override)
    # Vercel’s function filesystem is read-only except /tmp.
    if os.environ.get("VERCEL"):
        return Path("/tmp/night-desk")
    return Path(__file__).resolve().parent / "data"


def _bookings_path() -> Path:
    return _data_dir() / "bookings.json"


def _outbox_dir() -> Path:
    return _data_dir() / "outbox"


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
LOCK = threading.Lock()

DEMO_TODAY = date(2026, 9, 25)

HOTEL = {
    "name": "Tajmahal Hotel",
    "city": "New Delhi",
    "currency": "INR",
    "currency_spoken": "rupees",
    "desk_closes": "23:00",
    "today": DEMO_TODAY.isoformat(),
}

ROOMS = [
    {
        "id": "H101",
        "type": "Standard Queen",
        "guests": 2,
        "smoking": False,
        "rate": 8500,
        "open_nights": ["2026-09-25", "2026-09-26", "2026-09-27"],
    },
    {
        "id": "H102",
        "type": "Standard Queen",
        "guests": 2,
        "smoking": True,
        "rate": 8500,
        "open_nights": ["2026-09-25", "2026-09-26", "2026-09-27"],
    },
    {
        "id": "H201",
        "type": "Deluxe King",
        "guests": 2,
        "smoking": False,
        "rate": 14000,
        "open_nights": ["2026-09-25", "2026-09-26", "2026-09-27"],
    },
    {
        "id": "H202",
        "type": "Deluxe King",
        "guests": 3,
        "smoking": False,
        "rate": 15500,
        "open_nights": ["2026-09-26", "2026-09-27"],
    },
    {
        "id": "H301",
        "type": "Twin",
        "guests": 2,
        "smoking": False,
        "rate": 9500,
        "open_nights": ["2026-09-25", "2026-09-26", "2026-09-27"],
    },
    {
        "id": "H302",
        "type": "Twin",
        "guests": 2,
        "smoking": False,
        "rate": 9500,
        "open_nights": ["2026-09-26", "2026-09-27"],
    },
]

_QUOTES: dict[str, dict] = {}
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def today() -> date:
    raw = __import__("os").environ.get("DEMO_TODAY")
    if raw:
        return date.fromisoformat(raw)
    return DEMO_TODAY


def _load_bookings() -> list[dict]:
    path = _bookings_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save_bookings(rows: list[dict]) -> None:
    path = _bookings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2) + "\n")


def _nights_between(check_in: date, check_out: date) -> list[str]:
    nights = []
    day = check_in
    while day < check_out:
        nights.append(day.isoformat())
        day += timedelta(days=1)
    return nights


def _parse_one_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)
    base = today()
    if text in {"today", "tonight", "this evening", "this night"}:
        return base
    if text in {"tomorrow", "tomorrow night"}:
        return base + timedelta(days=1)
    for name, weekday in _WEEKDAYS.items():
        if name in text:
            delta = (weekday - base.weekday()) % 7
            return base + timedelta(days=delta)
    return None


def _stay(args: dict) -> tuple[Optional[date], Optional[date], Optional[str]]:
    check_in = _parse_one_date(
        args.get("check_in") or args.get("arrival") or args.get("from") or args.get("start")
    )
    check_out = _parse_one_date(
        args.get("check_out") or args.get("departure") or args.get("to") or args.get("end")
    )
    nights = args.get("nights")
    if check_in is None:
        return None, None, "I need a check-in date. Ask for tonight, Friday, or a YYYY-MM-DD date."
    if check_out is None:
        if nights:
            try:
                check_out = check_in + timedelta(days=int(nights))
            except (TypeError, ValueError):
                check_out = None
        if check_out is None:
            check_out = check_in + timedelta(days=1)
    if check_out <= check_in:
        return None, None, "Check-out must be after check-in. Ask for the leaving date."
    return check_in, check_out, None


def _truthy_smoking(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "yes", "smoking", "smoke"}:
        return True
    if text in {"false", "no", "non-smoking", "nonsmoking", "non smoking"}:
        return False
    return None


def _booked_nights(room_id: str, bookings: list[dict]) -> set[str]:
    taken: set[str] = set()
    for row in bookings:
        if row.get("room_id") == room_id and row.get("status") == "confirmed":
            taken.update(row.get("nights") or [])
    return taken


def _room_open(room: dict, nights: list[str], bookings: list[dict]) -> bool:
    open_nights = set(room["open_nights"])
    taken = _booked_nights(room["id"], bookings)
    return all(night in open_nights and night not in taken for night in nights)


def snapshot() -> dict:
    with LOCK:
        bookings = _load_bookings()
        inventory = []
        for room in ROOMS:
            taken = _booked_nights(room["id"], bookings)
            inventory.append({
                **{k: room[k] for k in ("id", "type", "guests", "smoking", "rate")},
                "open_nights": [n for n in room["open_nights"] if n not in taken],
            })
        return {
            "hotel": {**HOTEL, "today": today().isoformat()},
            "inventory": inventory,
            "bookings": bookings,
        }


def check_availability(args: dict) -> dict:
    check_in, check_out, err = _stay(args)
    if err:
        return {"error": err}
    try:
        guests = int(args.get("guests") or args.get("party_size") or 2)
    except (TypeError, ValueError):
        return {"error": "Guests must be a number. Ask how many people."}
    smoking = _truthy_smoking(args.get("smoking"))
    nights = _nights_between(check_in, check_out)
    with LOCK:
        bookings = _load_bookings()
        matches = []
        sold_out = []
        for room in ROOMS:
            if guests > room["guests"]:
                continue
            if smoking is True and not room["smoking"]:
                continue
            if smoking is False and room["smoking"]:
                continue
            item = {
                "room_id": room["id"],
                "room_type": room["type"],
                "guests_max": room["guests"],
                "smoking": room["smoking"],
                "rate_inr": room["rate"],
                "nights": len(nights),
                "total_inr": room["rate"] * len(nights),
            }
            if _room_open(room, nights, bookings):
                matches.append(item)
            else:
                next_open = next((n for n in room["open_nights"] if n >= today().isoformat()
                                  and n not in _booked_nights(room["id"], bookings)), None)
                sold_out.append({
                    "room_id": room["id"],
                    "room_type": room["type"],
                    "reason": "sold_out_for_those_nights",
                    "next_open_night": next_open,
                })
        cheapest = min((m["rate_inr"] for m in matches), default=None)
        return {
            "hotel": HOTEL["name"],
            "city": HOTEL["city"],
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "nights": len(nights),
            "guests": guests,
            "smoking_filter": smoking,
            "available": matches,
            "unavailable": sold_out,
            "cheapest_rate_inr": cheapest,
            "currency": "INR",
            "note": "Speak only these rates. Do not invent a cheaper price.",
        }


def quote_rate(args: dict) -> dict:
    room_id = str(args.get("room_id") or args.get("room") or "").upper()
    if not room_id:
        return {"error": "Need a room_id from check_availability, such as H201."}
    room = next((r for r in ROOMS if r["id"] == room_id), None)
    if not room:
        return {"error": f"Room {room_id} does not exist. Call check_availability again."}
    check_in, check_out, err = _stay(args)
    if err:
        return {"error": err}
    nights = _nights_between(check_in, check_out)
    with LOCK:
        bookings = _load_bookings()
        if not _room_open(room, nights, bookings):
            next_open = next((n for n in room["open_nights"] if n not in _booked_nights(room["id"], bookings)), None)
            return {
                "error": f"{room_id} is not open for those nights.",
                "next_open_night": next_open,
            }
        quote_id = _make_quote_id(room["id"], check_in, check_out)
        quote = {
            "quote_id": quote_id,
            "room_id": room["id"],
            "room_type": room["type"],
            "smoking": room["smoking"],
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "nights": nights,
            "night_count": len(nights),
            "rate_inr": room["rate"],
            "total_inr": room["rate"] * len(nights),
            "currency": "INR",
        }
        _QUOTES[quote_id] = quote
        return {**quote, "note": "Read these numbers back. Do not change them."}


def _make_quote_id(room_id: str, check_in: date, check_out: date) -> str:
    # Encodes the stay so create_booking still works after a serverless cold start.
    return f"Q-{room_id}-{check_in.strftime('%Y%m%d')}-{check_out.strftime('%Y%m%d')}"


def _quote_from_id(quote_id: str) -> Optional[dict]:
    cached = _QUOTES.get(quote_id)
    if cached:
        return cached
    match = re.fullmatch(r"Q-([A-Z0-9]+)-(\d{8})-(\d{8})", str(quote_id or "").upper())
    if not match:
        return None
    room_id, start, end = match.group(1), match.group(2), match.group(3)
    try:
        check_in = date(int(start[:4]), int(start[4:6]), int(start[6:8]))
        check_out = date(int(end[:4]), int(end[4:6]), int(end[6:8]))
    except ValueError:
        return None
    quoted = quote_rate({
        "room_id": room_id,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
    })
    return None if quoted.get("error") else quoted


def create_booking(args: dict) -> dict:
    guest_name = str(args.get("guest_name") or args.get("name") or "").strip()
    if not guest_name:
        return {"error": "Need the guest full name before booking."}
    confirmed = args.get("confirmed")
    if confirmed in (False, "false", "no"):
        return {"error": "Guest has not said yes. Do not book yet."}
    quote_id = str(args.get("quote_id") or "")
    quote = _quote_from_id(quote_id) if quote_id else None
    if quote is None:
        room_id = str(args.get("room_id") or "").upper()
        check_in, check_out, err = _stay(args)
        if err or not room_id:
            return {"error": "Call quote_rate first and then book with that quote_id after the guest says yes."}
        quoted = quote_rate({"room_id": room_id, "check_in": check_in.isoformat(), "check_out": check_out.isoformat()})
        if quoted.get("error"):
            return quoted
        quote = quoted
    with LOCK:
        bookings = _load_bookings()
        room = next((r for r in ROOMS if r["id"] == quote["room_id"]), None)
        if room is None or not _room_open(room, quote["nights"], bookings):
            return {"error": "That room was taken. Call check_availability again."}
        existing = {row["confirmation"] for row in bookings}
        confirmation = "ND-" + "".join(random.choices("0123456789", k=4))
        while confirmation in existing:
            confirmation = "ND-" + "".join(random.choices("0123456789", k=4))
        guest_email = _normalize_email(args.get("guest_email") or args.get("email"))
        booking = {
            "confirmation": confirmation,
            "guest_name": guest_name,
            "guest_email": guest_email,
            "hotel": HOTEL["name"],
            "city": HOTEL["city"],
            "room_id": quote["room_id"],
            "room_type": quote["room_type"],
            "smoking": quote["smoking"],
            "check_in": quote["check_in"],
            "check_out": quote["check_out"],
            "nights": quote["nights"],
            "night_count": quote["night_count"],
            "rate_inr": quote["rate_inr"],
            "total_inr": quote["total_inr"],
            "currency": "INR",
            "status": "confirmed",
        }
        mail = _deliver_booking_email(booking) if guest_email else {
            "email_status": "not_requested",
            "email_detail": "No guest email was given.",
        }
        booking["email_status"] = mail["email_status"]
        booking["email_detail"] = mail.get("email_detail", "")
        bookings.append(booking)
        _save_bookings(bookings)
        _save_outbox(booking, booking_voucher_html(booking), suffix=".html")
        spoken_mail = {
            "sent": "I emailed the full confirmation to that address.",
            "saved_local": "Email is not configured, so I could not send the letter.",
            "failed": "The confirmation email failed.",
            "invalid": "That email was not valid.",
            "not_requested": "",
        }.get(mail["email_status"], "")
        return {
            **booking,
            "print_url": f"/booking/{confirmation}",
            "download_url": f"/booking/{confirmation}/download",
            "note": "Read the confirmation code, room, dates, and total exactly. Do not invent another code. "
                    + spoken_mail
                    + " Tell them they can print or download the confirmation on the Stay tab now.",
        }


def get_booking(confirmation: str) -> Optional[dict]:
    code = str(confirmation or "").strip().upper()
    with LOCK:
        return next((row for row in _load_bookings() if row.get("confirmation") == code), None)


def booking_filename(booking: dict) -> str:
    return f"Tajmahal-{booking['confirmation']}.html"


def booking_voucher_html(booking: dict, auto_print: bool = False) -> str:
    esc = html.escape
    smoking = "smoking" if booking.get("smoking") else "non-smoking"
    print_js = "window.addEventListener('load', function () { window.print(); });" if auto_print else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Tajmahal Hotel {esc(booking['confirmation'])}</title>
<style>
  body {{ font-family: Georgia, serif; color: #1d1b16; background: #fdfcf8; margin: 0; padding: 32px; }}
  .voucher {{ max-width: 640px; margin: 0 auto; background: #fff; border: 1px solid #dad7cb;
              border-radius: 12px; padding: 32px; }}
  h1 {{ font-size: 28px; font-weight: 400; margin: 0 0 4px; }}
  .sub {{ color: #777673; margin-bottom: 24px; }}
  .code {{ font-family: ui-monospace, monospace; font-size: 22px; letter-spacing: 2px; color: #3923c7; }}
  dl {{ display: grid; grid-template-columns: 160px 1fr; gap: 8px 16px; }}
  dt {{ color: #a5a4a2; }} dd {{ margin: 0; }}
  .total {{ font-size: 20px; }}
  .actions {{ margin-top: 24px; display: flex; gap: 12px; }}
  .actions a {{ background: #3923c7; color: #fff; text-decoration: none; padding: 10px 18px;
                border-radius: 4px; font-family: ui-monospace, sans-serif; font-size: 13px; }}
  @media print {{
    body {{ background: #fff; padding: 0; }}
    .actions {{ display: none; }}
    .voucher {{ border: none; }}
  }}
</style>
</head>
<body>
  <article class="voucher">
    <h1>Tajmahal Hotel</h1>
    <p class="sub">New Delhi · Night desk booking confirmation</p>
    <p class="code">{esc(booking['confirmation'])}</p>
    <dl>
      <dt>Guest</dt><dd>{esc(str(booking.get('guest_name') or '-'))}</dd>
      <dt>Email</dt><dd>{esc(str(booking.get('guest_email') or '-'))}</dd>
      <dt>Room</dt><dd>{esc(booking.get('room_id', ''))} {esc(booking.get('room_type', ''))} ({smoking})</dd>
      <dt>Check-in</dt><dd>{esc(str(booking.get('check_in') or ''))}</dd>
      <dt>Check-out</dt><dd>{esc(str(booking.get('check_out') or ''))}</dd>
      <dt>Nights</dt><dd>{esc(str(booking.get('night_count') or ''))}</dd>
      <dt>Rate</dt><dd>INR {esc(str(booking.get('rate_inr') or ''))} per night</dd>
      <dt class="total">Total</dt><dd class="total">INR {esc(str(booking.get('total_inr') or ''))}</dd>
    </dl>
    <p class="sub">Front desk is closed after 11 PM. Show this confirmation at arrival.</p>
    <p class="actions">
      <a href="/booking/{esc(booking['confirmation'])}?print=1">Print</a>
      <a href="/booking/{esc(booking['confirmation'])}/download">Download</a>
    </p>
  </article>
  <script>{print_js}</script>
</body>
</html>
"""


def _normalize_email(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().lower()
    text = text.replace(" at ", "@").replace(" dot ", ".")
    text = re.sub(r"\s+", "", text)
    if not EMAIL_RE.match(text):
        return None
    return text


def _booking_email_text(booking: dict) -> str:
    smoking = "smoking" if booking.get("smoking") else "non-smoking"
    return (
        f"Tajmahal Hotel night desk — booking confirmation\n\n"
        f"Confirmation: {booking['confirmation']}\n"
        f"Guest: {booking['guest_name']}\n"
        f"Email: {booking.get('guest_email') or '-'}\n"
        f"Hotel: {booking['hotel']}, {booking['city']}\n"
        f"Room: {booking['room_id']} {booking['room_type']} ({smoking})\n"
        f"Check-in: {booking['check_in']}\n"
        f"Check-out: {booking['check_out']}\n"
        f"Nights: {booking['night_count']}\n"
        f"Rate: INR {booking['rate_inr']} per night\n"
        f"Total: INR {booking['total_inr']}\n\n"
        f"Front desk is closed after 11 PM. Reply to this email if you need to change the stay.\n"
    )


def _save_outbox(booking: dict, body: str, suffix: str = ".txt") -> Path:
    folder = _outbox_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{booking['confirmation']}{suffix}"
    path.write_text(body)
    return path


def _deliver_booking_email(booking: dict) -> dict:
    email = booking.get("guest_email")
    if not email:
        return {"email_status": "invalid", "email_detail": "Missing email."}
    body = _booking_email_text(booking)
    _save_outbox(booking, body)
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not host or not user or not password:
        return {
            "email_status": "saved_local",
            "email_detail": "SMTP is not set. Confirmation saved under data/outbox/.",
        }
    try:
        port = int(os.environ.get("SMTP_PORT") or 587)
        sender = os.environ.get("SMTP_FROM") or user
        msg = EmailMessage()
        msg["Subject"] = f"{booking['hotel']} confirmation {booking['confirmation']}"
        msg["From"] = sender
        msg["To"] = email
        msg.set_content(body)
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        return {"email_status": "sent", "email_detail": f"Sent to {email}."}
    except Exception as err:  # noqa: BLE001
        return {"email_status": "failed", "email_detail": str(err)}


TOOLS = {
    "check_availability": check_availability,
    "quote_rate": quote_rate,
    "create_booking": create_booking,
}


def run_tool(name: str, args: Optional[dict] = None) -> dict:
    handler = TOOLS.get(name)
    if handler is None:
        return {"error": f"Unknown tool {name}."}
    try:
        return handler(args or {})
    except Exception as err:  # noqa: BLE001 — surface a spoken recovery, not a stack
        return {"error": f"Tool {name} failed: {err}"}
