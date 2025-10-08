import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
import shutil
import pytz
from colorama import Fore, Style, init as colorama_init

import requests

# ======== KONFIGURASI UTAMA ========

BASE_ORIGIN = "https://game.digxe.com"
API_BASE = f"{BASE_ORIGIN}/api"

COOKIE_FILE = "cookie.txt"         # berisi cookie lengkap (copas dari DevTools)
CHECK_INTERVAL_MINUTES = 15        # interval cek reguler
NEARLY_DUE_MINUTES = 30            # kalau sisa < ini, interval dipersingkat
FAST_INTERVAL_SECONDS = 60         # interval cepat (menjelang claim)
TIMEDELTA_MINING_HOURS = 24        # durasi mining (server-side); hitung lokal
ALWAYS_LIVE_COUNTDOWN = True       # default: countdown realtime terus-menerus
COMPACT_COUNTDOWN = True           # tampilkan countdown pada satu baris (tanpa spam)
PROMPT_MANUAL_START = False        # tanya jam mulai mining saat start (dimatikan)

# state untuk log inline
_inline_active = False
_inline_prev_len = 0

# WIB timezone for log, per requested format
wib = pytz.timezone('Asia/Jakarta')
# initialize colorama for consistent colors
colorama_init(autoreset=False)


# In-memory state (tanpa file). Tidak ada digxe_state.json lagi.
STATE: dict = {}

# ======== UTIL ========

def utcnow():
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _clear_inline():
    global _inline_active, _inline_prev_len
    if _inline_active:
        sys.stdout.write("\r" + (" " * _inline_prev_len) + "\r")
        sys.stdout.flush()
        _inline_active = False
        _inline_prev_len = 0


def _ts_prefix() -> str:
    ts = datetime.now().astimezone(wib).strftime('%x %X %Z')
    return (
        f"{Fore.CYAN + Style.BRIGHT}[ {ts} ]{Style.RESET_ALL}"
        f"{Fore.WHITE + Style.BRIGHT} | {Style.RESET_ALL}"
    )


def log(msg: str):
    _clear_inline()
    print(f"{_ts_prefix()}{msg}", flush=True)


def log_inline(msg: str):
    """Tulis pesan pada satu baris (di-overwrite setiap update)."""
    global _inline_active, _inline_prev_len
    s = f"{_ts_prefix()}{msg}"
    cols = shutil.get_terminal_size((120, 20)).columns
    if len(s) > cols:
        s = s[:max(1, cols - 1)]
    pad = max(0, _inline_prev_len - len(s))
    sys.stdout.write("\r" + s + (" " * pad))
    sys.stdout.flush()
    _inline_active = True
    _inline_prev_len = len(s)


def load_cookie() -> str:
    if not os.path.exists(COOKIE_FILE):
        log(f"ERROR: {COOKIE_FILE} tidak ditemukan. Isi dengan cookie DevTools.")
        sys.exit(1)
    raw = open(COOKIE_FILE, "r", encoding="utf-8").read().strip()
    if not raw:
        log(f"ERROR: {COOKIE_FILE} kosong.")
        sys.exit(1)
    return raw


def load_state() -> dict:
    return STATE


def save_state(state: dict):
    global STATE
    STATE = state


def build_headers(cookie: str) -> dict:
    return {
        "authority": "game.digxe.com",
        "accept": "/",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "cookie": cookie,
        "origin": BASE_ORIGIN,
        "referer": f"{BASE_ORIGIN}/dashboard",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
            "(KHTML, seperti Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"
        ),
    }


def http_post(path: str, headers: dict, json_body=None, retries=3, backoff=3):
    url = f"{API_BASE}{path}"
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, headers=headers, json=json_body, timeout=30)
            # 429/503 → tunggu & retry
            if r.status_code in (429, 503):
                log(
                    f"HTTP {r.status_code} pada {path} (rate-limit/maintenance). "
                    f"Retry {attempt}/{retries}…"
                )
                time.sleep(backoff * attempt)
                continue
            return r
        except requests.RequestException as e:
            log(f"Request error {path}: {e}. Retry {attempt}/{retries}…")
            time.sleep(backoff * attempt)
    return None


def safe_text(resp: requests.Response) -> str:
    try:
        t = resp.text
        if len(t) > 500:
            return t[:500] + "…"
        return t
    except Exception:
        return "<no text>"


def _parse_remaining_from_text(text: str):
    """Parse sisa waktu dari teks server (mis. 'Come back in 23 hours ...').
    Mendukung hours/minutes/seconds jika ada. Kembalikan timedelta atau None.    """
    import re

    t = text.lower()
    h = m = s = 0
    mh = re.search(r"(\d+)\s*hour", t)
    if mh:
        h = int(mh.group(1))
    mm = re.search(r"(\d+)\s*minute", t)
    if mm:
        m = int(mm.group(1))
    ms = re.search(r"(\d+)\s*second", t)
    if ms:
        s = int(ms.group(1))
    if h == m == s == 0:
        return None
    return timedelta(hours=h, minutes=m, seconds=s)


def _countdown_until(target_dt: datetime):
    """Tampilkan hitungan mundur per detik sampai target_dt tercapai."""
    while True:
        now = utcnow()
        remaining = target_dt - now
        if remaining.total_seconds() <= 0:
            break
        total_sec = int(remaining.total_seconds())
        hh = total_sec // 3600
        mm = (total_sec % 3600) // 60
        ss = total_sec % 60
        if COMPACT_COUNTDOWN:
            log_inline(f"Klaim dalam {hh:02d}:{mm:02d}:{ss:02d}")
        else:
            log(f"Klaim dalam {hh:02d}:{mm:02d}:{ss:02d}")
        time.sleep(1)
    # bersihkan baris inline setelah selesai agar log berikutnya tidak tumpang tindih
    _clear_inline()


_asked_manual = False


def _maybe_prompt_manual_start_time():
    """Tanya user jam mulai mining (WIB). Format: HH:MM atau HH.MM. Kosongkan untuk lewati."""
    global _asked_manual
    if _asked_manual:
        return
    _asked_manual = True

    try:
        prompt = (
            f"{Fore.WHITE + Style.BRIGHT}Masukkan jam mulai mining terakhir (WIB)"
            f" [format HH:MM atau HH.MM, kosongkan untuk lewati]: {Style.RESET_ALL}"
        )
        raw = input(prompt).strip()
    except Exception:
        return

    if not raw:
        return

    val = raw.replace(" ", "").replace(".", ":")
    parts = val.split(":")
    try:
        if len(parts) == 1:
            hh = int(parts[0])
            mm = 0
        else:
            hh = int(parts[0])
            mm = int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("range")
    except Exception:
        log(f"{Fore.RED + Style.BRIGHT}Format jam tidak valid. Lewati sinkron manual.{Style.RESET_ALL}")
        return

    now_wib = datetime.now().astimezone(wib)
    dt_wib = now_wib.replace(hour=hh, minute=mm, second=0, microsecond=0)
    # jika waktu input lebih besar dari sekarang (belum terjadi hari ini), anggap kemarin
    if dt_wib > now_wib:
        dt_wib = dt_wib - timedelta(days=1)

    # simpan sebagai UTC ISO
    last_start_utc = dt_wib.astimezone(timezone.utc)
    state = load_state()
    state["last_start"] = iso(last_start_utc)
    save_state(state)

    next_claim_wib = dt_wib + timedelta(hours=TIMEDELTA_MINING_HOURS)
    rem = next_claim_wib - now_wib
    total = int(rem.total_seconds())
    if total < 0:
        total = 0
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    log(
        f"{Fore.GREEN + Style.BRIGHT}Sinkron manual diset.{Style.RESET_ALL} "        f"Target klaim @ {Fore.WHITE + Style.BRIGHT}{next_claim_wib.strftime('%x %X %Z')}{Style.RESET_ALL} "
        f"(sisa {h:02d}:{m:02d}:{s:02d})"
    )


# ======== AKSI API ========

def start_mining(headers: dict) -> bool:
    r = http_post("/mining/start", headers)
    if r is None:
        log("Gagal start: tidak ada respons setelah retry.")
        return False
    if not r.ok:
        # Coba baca pesan dari server
        body_text = None
        try:
            data = r.json()
            body_text = str(data.get("error") or data.get("message") or data)        except Exception:
            body_text = safe_text(r)

        # Jika sesi sudah aktif, sinkronkan state lokal dan tampilkan hitung mundur
        if r.status_code == 400 and body_text and "already active" in body_text.lower():
            # Sesuai permintaan: anggap klaim dalam 24 jam penuh dari sekarang
            remaining = timedelta(hours=TIMEDELTA_MINING_HOURS)
            next_claim_at = utcnow() + remaining
            last_start_est = next_claim_at - timedelta(hours=TIMEDELTA_MINING_HOURS)
            state = load_state()
            state["last_start"] = iso(last_start_est)
            save_state(state)

            total_sec = int(remaining.total_seconds())
            hh = total_sec // 3600
            mm = (total_sec % 3600) // 60
            ss = total_sec % 60
            log(
                f"{Fore.GREEN + Style.BRIGHT}Sudah mining.{Style.RESET_ALL} "                f"Klaim dalam {Fore.WHITE + Style.BRIGHT}{hh:02d}:{mm:02d}:{ss:02d}{Style.RESET_ALL}"
            )
            # Anggap sukses agar loop utama langsung memakai state yang telah disinkronkan
            return True

        if r.status_code in (401, 403):
            log("Unauthorized/Forbidden. Cookie kemungkinan kadaluarsa. Perbarui cookie.txt.")
        else:
            # Rapikan log error umum tanpa menampilkan body mentah
            log(f"Start mining gagal: HTTP {r.status_code}.")
        return False
    try:
        data = r.json()
    except Exception:
        data = {}
    if data.get("success") is True:
        state = load_state()
        state["last_start"] = iso(utcnow())
        save_state(state)
        log(f"{Fore.GREEN + Style.BRIGHT}✅ Mining dimulai.{Style.RESET_ALL} Timestamp disimpan.")
        return True
    log("Start mining tidak success.")
    return False


def claim(headers: dict) -> bool:
    r = http_post("/claim", headers)
    if r is None:
        log("Gagal claim: tidak ada respons setelah retry.")
        return False
    log(f"Claim → HTTP {r.status_code} | body: {safe_text(r)}")
    if not r.ok:
        if r.status_code in (401, 403):
            log("Unauthorized/Forbidden saat claim. Cookie kemungkinan kadaluarsa. Perbarui cookie.txt.")
        return False
    amt = None
    try:
        data = r.json()
        amt = data.get("claimed")
    except Exception:
        data = {}
    if data.get("success") is True:
        state = load_state()
        state["last_claim"] = iso(utcnow())
        state["last_claim_amount"] = amt
        save_state(state)
        log(f"✅ Claim sukses. claimed={amt}")
        return True
    log("Claim tidak success.")
    return False


# ======== LOOP UTAMA ========

def main():
    # Allow enabling live countdown via env var or CLI flag
    global ALWAYS_LIVE_COUNTDOWN
    if os.getenv("DIGXE_LIVE", "").lower() in ("1", "true", "yes"):
        ALWAYS_LIVE_COUNTDOWN = True
    if any(arg in ("--live", "-l") for arg in sys.argv[1:]):
        ALWAYS_LIVE_COUNTDOWN = True

    log("Start… Digxe auto start/claim")
    while True:
        cookie = load_cookie()
        headers = build_headers(cookie)
        state = load_state()

        # Jika belum pernah start → langsung coba start tanpa log tambahan
        if "last_start" not in state:
            ok = start_mining(headers)
            if not ok:
                # gagal start, tunggu sebentar dan coba lagi
                time.sleep(30)
                continue
            # berhasil start atau sudah aktif → lanjut hitung mundur di loop ini
            state = load_state()

        last_start = parse_iso(state["last_start"])
        next_claim_at = last_start + timedelta(hours=TIMEDELTA_MINING_HOURS)
        now = utcnow()

        if now >= next_claim_at:
            log("Waktu claim telah tiba → mencoba claim…")
            # reload cookie sebelum claim untuk jaga-jaga
            headers = build_headers(load_cookie())
            if claim(headers):
                # Setelah claim sukses → langsung start lagi
                log("Auto start mining baru…")
                start_mining(headers)
                # tidur sebentar agar tidak spam
                time.sleep(FAST_INTERVAL_SECONDS)
            else:
                # gagal claim → coba lagi nanti
                time.sleep(FAST_INTERVAL_SECONDS)
            continue

        # Belum waktunya claim → tampilkan sisa waktu
        remaining = next_claim_at - now
        total_sec = int(remaining.total_seconds())
        hh = total_sec // 3600
        mm = (total_sec % 3600) // 60
        ss = total_sec % 60
        if not ALWAYS_LIVE_COUNTDOWN:
            log(f"Klaim dalam {hh:02d}:{mm:02d}:{ss:02d}")

        # Mode countdown realtime terus-menerus
        if ALWAYS_LIVE_COUNTDOWN:
            _countdown_until(next_claim_at)
            log("Waktu claim telah tiba → mencoba claim…")
            headers = build_headers(load_cookie())
            if claim(headers):
                log("Auto start mining baru…")
                start_mining(headers)
                time.sleep(FAST_INTERVAL_SECONDS)
            else:
                time.sleep(FAST_INTERVAL_SECONDS)
            continue

        # Countdown realtime hanya saat mendekati waktu klaim
        if remaining <= timedelta(minutes=NEARLY_DUE_MINUTES):
            _countdown_until(next_claim_at)
            log("Waktu claim telah tiba → mencoba claim…")
            headers = build_headers(load_cookie())
            if claim(headers):
                log("Auto start mining baru…")
                start_mining(headers)
                time.sleep(FAST_INTERVAL_SECONDS)
            else:
                time.sleep(FAST_INTERVAL_SECONDS)
            continue

        # Jika masih lama, tidur lebih lama agar tidak spam
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Dihentikan oleh user.")
