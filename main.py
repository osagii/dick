#!/usr/bin/env python3

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
import shutil
import pytz
from colorama import Fore, Style, init as colorama_init
try:
    # Optional: derive address from Ethereum private key
    from eth_account import Account  # type: ignore
    _HAS_ETH_ACCOUNT = True
except Exception:
    _HAS_ETH_ACCOUNT = False
try:
    from web3 import Web3  # type: ignore
    _HAS_WEB3 = True
except Exception:
    _HAS_WEB3 = False

import requests

# ======== KONFIGURASI UTAMA ========

BASE_ORIGIN = "https://game.digxe.com"
API_BASE = f"{BASE_ORIGIN}/api"

COOKIE_FILE = "cookie.txt"         # berisi cookie lengkap (copas dari DevTools)
CHECK_INTERVAL_MINUTES = 15        # interval cek reguler
NEARLY_DUE_MINUTES = 30            # kalau sisa < ini, interval dipersingkat
FAST_INTERVAL_SECONDS = 60         # interval cepat (menjelang claim)
TIMEDELTA_MINING_HOURS = 24        # durasi mining (server-side); hitung lokal
ALWAYS_LIVE_COUNTDOWN = True       # default: countdown realtime terus-menerus (single akun)
COMPACT_COUNTDOWN = True           # tampilkan countdown pada satu baris (tanpa spam)
PROMPT_MANUAL_START = False        # tanya jam mulai mining saat start (dimatikan)

# ======== KONFIG WALLET AUTO-CLAIM ========
PV_FILE = "pv.txt"  # private key file; bisa berisi '0x..' atau 'PRIVATE_KEY=..'
WALLET_GET_SIGNATURE_PATH = "/wallet/get-signature"
# Tebakan endpoint withdraw, ubah jika berbeda:
WALLET_WITHDRAW_PATH = "/wallet/withdraw"
# Maksimal percobaan per hari mengikuti server (withdrawalInfo), namun
# kita batasi sesuai permintaan: maksimal 2x klaim masing-masing 10%.
WALLET_AUTO_SLEEP_AFTER_CLAIM_SEC = 5
TXHASH_FILE = "txhash.txt"  # opsional: berisi tx hash bila backend minta

# Non-interactive mode: tidak ada input() sama sekali
NON_INTERACTIVE = os.getenv("DIGXE_NO_ASK", "").lower() in ("1", "true", "yes") or any(
    arg in ("--no-ask", "--auto", "--wallet-auto") for arg in sys.argv[1:]
)

# On-chain config via env/file
RPC_URL = os.getenv("RPC_URL", "").strip()
CHAIN_ID = int(os.getenv("CHAIN_ID", "0") or 0)
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS", "").strip()
CONTRACT_ABI_PATH = os.getenv("CONTRACT_ABI", "contract_abi.json").strip()
CONTRACT_METHOD = os.getenv("CONTRACT_METHOD", "withdraw").strip()

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
    # Jangan potong string agar HH:MM:SS tidak ter-truncate
    pad = max(0, _inline_prev_len - len(s))
    sys.stdout.write("\r" + s + (" " * pad))
    sys.stdout.flush()
    _inline_active = True
    _inline_prev_len = len(s)


def load_cookie() -> str:
    """Back-compat: ambil baris pertama dari cookie.txt."""
    cookies = load_cookies_list()
    if not cookies:
        log(f"ERROR: {COOKIE_FILE} kosong atau tidak ditemukan.")
        sys.exit(1)
    return cookies[0]


def load_cookies_list() -> list[str]:
    """Baca cookie.txt sebagai multi-akun.
    Format yang sudah ada: satu baris = satu cookie string utuh.
    Baris kosong atau diawali '#' diabaikan.
    """
    if not os.path.exists(COOKIE_FILE):
        return []
    lines = []
    with open(COOKIE_FILE, "r", encoding="utf-8") as f:
        for ln in f:
            s = (ln or "").strip()
            if not s or s.startswith("#"):
                continue
            lines.append(s)
    return lines


def load_state() -> dict:
    return STATE


def save_state(state: dict):
    global STATE
    STATE = state


def build_headers(cookie: str, referer_path: str = "/dashboard") -> dict:
    return {
        "authority": "game.digxe.com",
        "accept": "/",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "cookie": cookie,
        "origin": BASE_ORIGIN,
        "referer": f"{BASE_ORIGIN}{referer_path}",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
            "(KHTML, seperti Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"
        ),
    }


def build_page_headers(cookie: str, referer_path: str = "/mining") -> dict:
    h = build_headers(cookie, referer_path)
    # Tuning header agar Next.js kirim RSC payload
    h.update({
        "accept": "text/x-component, */*",
        "rsc": "1",
    })
    return h


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


def http_get(url: str, headers: dict, retries=2, backoff=2):
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if r.status_code in (429, 503):
                time.sleep(backoff * attempt)
                continue
            return r
        except requests.RequestException:
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
    Mendukung hours/minutes/seconds jika ada. Kembalikan timedelta atau None.
    """
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


def _parse_balance_from_rsc(text: str):
    """Ambil dxeBalance dari payload RSC mining menggunakan regex sederhana."""
    import re
    m = re.search(r'"dxeBalance"\s*:\s*([0-9]+(?:\.[0-9]+)?)', text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _parse_device_seconds_from_rsc(text: str):
    """Ambil semua seconds_remaining dan secondsRemaining dari payload RSC mining."""
    import re
    secs = []
    for pat in (r'"seconds_remaining"\s*:\s*(\d+)', r'"secondsRemaining"\s*:\s*(\d+)'):
        for m in re.finditer(pat, text):
            try:
                secs.append(int(m.group(1)))
            except Exception:
                pass
    # Dedup dan urutkan
    secs = sorted(set(secs))
    return secs


def _parse_last_claim_time_ms(text: str):
    """Ambil lastClaimTime epoch-ms dari payload RSC dashboard."""
    import re
    m = re.search(r'"lastClaimTime"\s*:\s*(\d{10,13})', text)
    if not m:
        return None
    try:
        v = int(m.group(1))
        # jika detik (10 digit), konversi ke ms
        if v < 10_000_000_000:  # improbable but safe
            v *= 1000
        return v
    except Exception:
        return None


def _server_now_ms() -> int:
    """Ambil waktu server via HEAD Date header untuk akurasi lintas perangkat."""
    try:
        r = requests.head(BASE_ORIGIN, timeout=10)
        d = r.headers.get("date") or r.headers.get("Date")
        if d:
            return int(datetime.strptime(d, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        pass
    return int(utcnow().timestamp() * 1000)


def fetch_dashboard_last_claim_ms(cookie: str) -> int | None:
    """GET /dashboard (RSC) dan ekstrak lastClaimTime (ms)."""
    headers = build_page_headers(cookie, "/dashboard")
    try:
        r = http_get(f"{BASE_ORIGIN}/dashboard", headers)
        if r is None or not r.ok:
            return None
        return _parse_last_claim_time_ms(r.text)
    except Exception:
        return None


def _countdown_until(target_dt: datetime, device_deadlines: list | None = None):
    """Tampilkan hitungan mundur per detik sampai target_dt tercapai.
    Jika device_deadlines diberikan (list of datetime), maka baris countdown juga
    menampilkan device-min countdown, dan akan berhenti lebih awal jika ada device
    yang mencapai 0.
    """
    while True:
        now = utcnow()
        remaining = target_dt - now
        # Hitung device min (informational only)
        dev_extra = ""
        if device_deadlines:
            mins = []
            for dl in device_deadlines:
                rem = max(0, int((dl - now).total_seconds()))
                mins.append(rem)
            if mins:
                m = min(mins)
                dh = m // 3600
                dm = (m % 3600) // 60
                ds = m % 60
                dev_extra = f" | Device min {dh:02d}:{dm:02d}:{ds:02d}"
        if remaining.total_seconds() <= 0:
            break
        total_sec = int(remaining.total_seconds())
        hh = total_sec // 3600
        mm = (total_sec % 3600) // 60
        ss = total_sec % 60
        if COMPACT_COUNTDOWN:
            log_inline(f"Claim dalam {hh:02d}:{mm:02d}:{ss:02d}{dev_extra}")
        else:
            log(f"Claim dalam {hh:02d}:{mm:02d}:{ss:02d}{dev_extra}")
        time.sleep(1)
    _clear_inline()


def _format_hms(total_sec: int) -> str:
    hh = total_sec // 3600
    mm = (total_sec % 3600) // 60
    ss = total_sec % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _multi_countdown_until(targets: list[tuple[str, datetime]], base_server_ms: int | None = None):
    """Live countdown multi-akun pada satu baris.
    targets: list of (label, next_claim_at_utc)
    base_server_ms: anchor waktu server (ms) untuk menghindari drift jam lokal.
    Loop berhenti saat target tercepat mencapai 0.
    """
    if not targets:
        return
    t0 = time.time()
    base_ms = base_server_ms or int(utcnow().timestamp() * 1000)
    while True:
        now_ms = base_ms + int((time.time() - t0) * 1000)
        now_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        parts = []
        min_rem = None
        for label, next_dt in targets:
            rem = max(0, int((next_dt - now_dt).total_seconds()))
            parts.append(f"[{label}] Claim dalam { _format_hms(rem) }")
            if min_rem is None or rem < min_rem:
                min_rem = rem
        log_inline(" | ".join(parts))
        if min_rem is None or min_rem <= 0:
            break
        time.sleep(1)
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
        f"{Fore.GREEN + Style.BRIGHT}Sinkron manual diset.{Style.RESET_ALL} "
        f"Target klaim @ {Fore.WHITE + Style.BRIGHT}{next_claim_wib.strftime('%x %X %Z')}{Style.RESET_ALL} "
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
            body_text = str(data.get("error") or data.get("message") or data)
        except Exception:
            body_text = safe_text(r)

        # Jika sesi sudah aktif, sinkronkan state lokal dan tampilkan hitung mundur
        if r.status_code == 400 and body_text and "already active" in body_text.lower():
            # Coba baca sisa waktu dari pesan server (e.g., "Come back in 5 hours 12 minutes")
            rem = _parse_remaining_from_text(body_text) or timedelta(hours=TIMEDELTA_MINING_HOURS)
            next_claim_at = utcnow() + rem
            last_start_est = next_claim_at - timedelta(hours=TIMEDELTA_MINING_HOURS)
            state = load_state()
            state["last_start"] = iso(last_start_est)
            save_state(state)

            total_sec = int(rem.total_seconds())
            hh = total_sec // 3600
            mm = (total_sec % 3600) // 60
            ss = total_sec % 60
            log(
                f"{Fore.GREEN + Style.BRIGHT}Sudah mining.{Style.RESET_ALL} "
                f"Claim dalam {Fore.WHITE + Style.BRIGHT}{hh:02d}:{mm:02d}:{ss:02d}{Style.RESET_ALL}"
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
        # Gunakan waktu server dari header Date agar akurat
        srv_ms = None
        try:
            d = r.headers.get("date") or r.headers.get("Date")
            if d:
                srv_ms = int(datetime.strptime(d, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc).timestamp() * 1000)
        except Exception:
            srv_ms = None
        ts = datetime.fromtimestamp((srv_ms or int(utcnow().timestamp()*1000))/1000, tz=timezone.utc)
        state = load_state()
        state["last_start"] = iso(ts)
        save_state(state)
        log(f"{Fore.GREEN + Style.BRIGHT}✅ Mining dimulai.{Style.RESET_ALL}")
        return True
    log("Start mining tidak success.")
    return False


def claim(headers: dict) -> bool:
    r = http_post("/claim", headers)
    if r is None:
        log("Gagal claim: tidak ada respons setelah retry.")
        return False
    # detail HTTP sengaja tidak ditampilkan agar output ringkas
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
        # Setelah klaim, coba tampilkan saldo total dari halaman mining
        try:
            cookie = load_cookie()
            pg_headers = build_page_headers(cookie, "/mining")
            r = http_get(f"{BASE_ORIGIN}/mining", pg_headers)
            if r is not None and r.ok:
                bal = _parse_balance_from_rsc(r.text)
                if bal is not None:
                    log(f"Saldo total sekarang: {bal}")
        except Exception:
            pass
        return True
    log("Claim tidak success.")
    return False


# ======== WALLET AUTO-CLAIM (BARU) ========

def _read_private_key_and_address():
    """Baca private key dari PV_FILE dan coba deteksi wallet address jika ada.
    Mendukung format:
      - baris tunggal: 0x...
      - KEY=VALUE, mis. PRIVATE_KEY=0x..., WALLET_ADDRESS=0x...
      - beberapa baris; script akan mencari pola hex 0x.. untuk private key dan address.
    """
    if not os.path.exists(PV_FILE):
        return None, None
    pk = None
    addr = None
    try:
        with open(PV_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except Exception:
        return None, None

    for line in content.splitlines():
        s = line.strip()
        if not s:
            continue
        lower = s.lower()
        if "wallet_address" in lower or "address" in lower:
            parts = s.split("=", 1)
            cand = parts[1].strip() if len(parts) == 2 else s
            if cand.startswith("0x") and len(cand) >= 42:
                addr = cand[:42]
        elif "private_key" in lower or "privkey" in lower or "key" in lower:
            parts = s.split("=", 1)
            cand = parts[1].strip() if len(parts) == 2 else s
            if cand.startswith("0x") and len(cand) >= 66:
                pk = cand[:66]
            elif len(cand) == 64 and all(c in "0123456789abcdefABCDEF" for c in cand):
                pk = "0x" + cand
        else:
            if s.startswith("0x") and len(s) >= 66:
                pk = s[:66]
            elif s.startswith("0x") and len(s) >= 42:
                addr = s[:42]

    return pk, addr


def _ensure_wallet_address(pk, existing_addr):
    """Pastikan ada wallet address. Jika tidak diketahui, minta input user.
    (Tanpa dependensi Web3 untuk derivasi dari pk.)
    """
    if existing_addr and existing_addr.startswith("0x") and len(existing_addr) >= 42:
        return existing_addr[:42]

    # Coba derive dari private key jika library tersedia
    if _HAS_ETH_ACCOUNT and pk:
        try:
            acct = Account.from_key(pk)
            addr = acct.address
            # Persist ke pv.txt agar tidak ditanya lagi
            try:
                if os.path.exists(PV_FILE):
                    with open(PV_FILE, "a", encoding="utf-8") as f:
                        f.write(f"\nWALLET_ADDRESS={addr}\n")
                else:
                    with open(PV_FILE, "w", encoding="utf-8") as f:
                        f.write(f"WALLET_ADDRESS={addr}\n")
            except Exception:
                pass
            return addr
        except Exception:
            pass
    if NON_INTERACTIVE:
        return None
    try:
        prompt = (
            f"{Fore.WHITE + Style.BRIGHT}Masukkan wallet address (0x…): {Style.RESET_ALL}"
        )
        s = input(prompt).strip()
        if s.startswith("0x") and len(s) >= 42:
            addr = s[:42]
            # Persist ke pv.txt agar tidak ditanya lagi
            try:
                if os.path.exists(PV_FILE):
                    with open(PV_FILE, "a", encoding="utf-8") as f:
                        f.write(f"\nWALLET_ADDRESS={addr}\n")
                else:
                    with open(PV_FILE, "w", encoding="utf-8") as f:
                        f.write(f"WALLET_ADDRESS={addr}\n")
            except Exception:
                pass
            return addr
    except Exception:
        return None
    return None


def wallet_get_signature(headers: dict, wallet_address: str):
    body = {"walletAddress": wallet_address}
    r = http_post(WALLET_GET_SIGNATURE_PATH, headers, json_body=body)
    if r is None:
        log("Gagal get-signature: tidak ada respons setelah retry.")
        return None
    try:
        data = r.json()
    except Exception:
        log(f"get-signature → HTTP {r.status_code} | body: {safe_text(r)}")
        return None
    if not data or not data.get("success"):
        log(f"get-signature gagal/invalid. HTTP {r.status_code}.")
        return None
    return data


def wallet_withdraw(headers: dict, wallet_address: str, sig_data: dict, pk) -> bool:
    """Kirim permintaan withdraw/claim ke endpoint wallet.
    Jika endpoint berbeda, ubah konstanta WALLET_WITHDRAW_PATH.
    Payload ini mengikuti pola umum: address + amount + nonce + deadline + signature.
    """
    payload = {
        "walletAddress": wallet_address,
        "amount": sig_data.get("amount"),
        "nonce": sig_data.get("nonce"),
        "deadline": sig_data.get("deadline"),
        "signature": sig_data.get("signature"),
    }
    # Coba beberapa endpoint (404 fallback)
    candidates = [WALLET_WITHDRAW_PATH, "/wallet/claim"]
    last_r = None
    for path in candidates:
        r = http_post(path, headers, json_body=payload)
        last_r = r
        if r is None:
            continue
        if r.status_code == 404:
            continue
        # Jika butuh tx hash, coba dapatkan lalu kirim ulang
        need_txhash = False
        if r.status_code == 400:
            try:
                err = r.json()
                msg = str(err)
            except Exception:
                msg = safe_text(r)
            if msg and ("transaction hash" in msg.lower() or "tx hash" in msg.lower()):
                need_txhash = True
        if need_txhash:
            # Auto obtain tx hash (no prompt if NON_INTERACTIVE)
            txh = _obtain_tx_hash(pk or "", sig_data)
            if not txh:
                continue
            payload2 = dict(payload)
            payload2["transactionHash"] = txh
            payload2["txHash"] = txh
            r2 = http_post(path, headers, json_body=payload2)
            last_r = r2
            if r2 and r2.ok:
                try:
                    data2 = r2.json()
                except Exception:
                    data2 = None
                if data2 and (data2.get("success") is True or data2.get("status") in ("ok", "success")):
                    claimed = data2.get("claimed") or data2.get("amount") or sig_data.get("claimableAmount")
                    log(f"{Fore.GREEN + Style.BRIGHT}✅ Withdraw/claim sukses.{Style.RESET_ALL} amount={claimed}")
                    return True
            # jika gagal, lanjut kandidat berikutnya
            continue
        ok = r.ok
        try:
            data = r.json()
        except Exception:
            data = None
        if ok and data and (data.get("success") is True or data.get("status") in ("ok", "success")):
            claimed = data.get("claimed") or data.get("amount") or sig_data.get("claimableAmount")
            log(f"{Fore.GREEN + Style.BRIGHT}✅ Withdraw/claim sukses.{Style.RESET_ALL} amount={claimed}")
            return True

    if last_r is None:
        log("Gagal withdraw: tidak ada respons setelah retry.")
    else:
        log(f"Withdraw gagal → HTTP {last_r.status_code} | body: {safe_text(last_r)}")
    return False


def _obtain_tx_hash(pk, sig_data: dict):
    """Dapatkan tx hash tanpa tanya user bila memungkinkan.
    Urutan: env TX_HASH -> file txhash.txt -> broadcast on-chain (jika RPC & ABI tersedia) -> (prompt jika interaktif).
    """
    # 1) Env override
    txh = os.getenv("TX_HASH", "").strip()
    if txh:
        return txh
    # 2) File fallback
    try:
        if os.path.exists(TXHASH_FILE):
            with open(TXHASH_FILE, "r", encoding="utf-8") as f:
                s = f.read().strip()
                if s:
                    return s
    except Exception:
        pass
    # 3) Try broadcast on-chain
    txh = _broadcast_onchain_and_get_hash(pk, sig_data)
    if txh:
        # simpan agar run berikutnya tidak perlu ulang
        try:
            with open(TXHASH_FILE, "w", encoding="utf-8") as f:
                f.write(txh + "\n")
        except Exception:
            pass
        return txh
    # 4) Last resort: prompt only if allowed
    if NON_INTERACTIVE:
        return None
    try:
        s = input(f"{Fore.WHITE + Style.BRIGHT}Masukkan transaction hash (0x…): {Style.RESET_ALL}").strip()
        if s.startswith("0x") and len(s) >= 66:
            return s
    except Exception:
        return None
    return None


def _load_contract_abi():
    try:
        if os.path.exists(CONTRACT_ABI_PATH):
            with open(CONTRACT_ABI_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return None
    # also allow ABI via env (JSON string)
    abi_env = os.getenv("CONTRACT_ABI_JSON", "").strip()
    if abi_env:
        try:
            return json.loads(abi_env)
        except Exception:
            return None
    return None


def _broadcast_onchain_and_get_hash(pk, sig_data: dict):
    if not _HAS_WEB3 or not _HAS_ETH_ACCOUNT:
        return None
    if not RPC_URL or not CONTRACT_ADDRESS or CHAIN_ID <= 0:
        return None
    try:
        w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 20}))
        acct = Account.from_key(pk)
        abi = _load_contract_abi()
        if not abi:
            return None
        contract = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=abi)
        func = getattr(contract.functions, CONTRACT_METHOD, None)
        if func is None:
            return None
        # assume signature (amount, nonce, deadline, signature)
        call = func(
            int(sig_data.get("amount", 0)),
            int(sig_data.get("nonce", 0)),
            int(sig_data.get("deadline", 0)),
            bytes.fromhex(sig_data.get("signature", "")[2:]) if str(sig_data.get("signature", "")).startswith("0x") else sig_data.get("signature", ""),
        )
        tx = call.build_transaction({
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gasPrice": w3.eth.gas_price,
            "chainId": CHAIN_ID,
        })
        # Estimate gas safely
        try:
            gas_est = w3.eth.estimate_gas(tx)
            tx["gas"] = int(gas_est * 1.2)
        except Exception:
            tx["gas"] = tx.get("gas", 300000)
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        return tx_hash.hex()
    except Exception:
        return None


def run_wallet_auto_claim():
    """Jalankan proses auto-claim wallet: maks 2x, masing-masing 10%.
    Mengikuti info dari endpoint get-signature.
    """
    cookie = load_cookie()
    headers = build_headers(cookie, referer_path="/wallet")

    pk, addr_from_file = _read_private_key_and_address()
    if not pk:
        log(f"{Fore.RED + Style.BRIGHT}Private key tidak ditemukan di {PV_FILE}.{Style.RESET_ALL}")
        return
    addr = _ensure_wallet_address(pk, addr_from_file)
    if not addr:
        log(f"{Fore.RED + Style.BRIGHT}Wallet address belum diset. Batalkan.{Style.RESET_ALL}")
        return

    info = wallet_get_signature(headers, addr)
    if not info:
        return

    try:
        wi = info.get("withdrawalInfo") or {}
        remaining = int(wi.get("remainingWithdrawals") or 0)
        total_balance = float(info.get("totalBalance") or 0)
        claimable_amount = float(info.get("claimableAmount") or 0)
    except Exception:
        remaining = 0
        total_balance = 0.0
        claimable_amount = 0.0

    log(
        f"Wallet: {addr} | balance={total_balance} | claimable={claimable_amount} | remainingWithdrawals={remaining}"
    )

    if remaining <= 0 or claimable_amount <= 0:
        log("Tidak ada kuota klaim atau claimable 0. Selesai.")
        return

    # Target per transaksi: 10% dari total saldo, namun server sudah memberi 'claimableAmount'.
    target_per_tx = min(claimable_amount, total_balance * 0.10)
    _ = target_per_tx  # saat ini payload mengikuti nilai dari server

    to_do = min(2, remaining)
    for i in range(to_do):
        sig = wallet_get_signature(headers, addr)
        if not sig:
            break
        try:
            this_claimable = float(sig.get("claimableAmount") or 0)
        except Exception:
            this_claimable = 0.0
        if this_claimable <= 0:
            log("claimableAmount=0 pada refresh signature. Berhenti.")
            break

        ok = wallet_withdraw(headers, addr, sig, pk)
        if not ok:
            break
        if i < to_do - 1:
            time.sleep(WALLET_AUTO_SLEEP_AFTER_CLAIM_SEC)


# ======== LOOP UTAMA ========

def main():
    # Allow enabling live countdown via env var or CLI flag
    global ALWAYS_LIVE_COUNTDOWN
    if os.getenv("DIGXE_LIVE", "").lower() in ("1", "true", "yes"):
        ALWAYS_LIVE_COUNTDOWN = True
    if any(arg in ("--live", "-l") for arg in sys.argv[1:]):
        ALWAYS_LIVE_COUNTDOWN = True

    # Tentukan mode: single-akun (baris 1) vs multi-akun (>=2 baris)
    cookies = load_cookies_list()
    if not cookies:
        log(f"ERROR: {COOKIE_FILE} kosong atau tidak ditemukan.")
        sys.exit(1)

    single_mode = len(cookies) == 1
    # Konfigurasi live countdown untuk multi-akun via env/flag
    MULTI_LIVE = (
        os.getenv("DIGXE_MULTI_LIVE", "").lower() in ("1", "true", "yes") or
        any(arg in ("--live-all", "--live") for arg in sys.argv[1:]) or
        ALWAYS_LIVE_COUNTDOWN  # izinkan jika sudah diaktifkan global
    )
    if not single_mode and not MULTI_LIVE:
        # Default: matikan live untuk multi-akun jika tidak diminta
        log("Mode multi-akun terdeteksi → live countdown dimatikan, cek periodik.")
        ALWAYS_LIVE_COUNTDOWN = False

    log("Start… Digxe auto start/claim")
    first_run = True
    while True:
        earliest_ms = None
        srv_ms_loop = _server_now_ms()
        now_dt_loop = datetime.fromtimestamp(srv_ms_loop/1000, tz=timezone.utc)
        targets: list[tuple[str, datetime]] = []

        for idx, ck in enumerate(cookies, start=1):
            label = f"Account {idx}"
            headers = build_headers(ck)

            # Percobaan awal: coba klaim sekali (tanpa log berlebih), lalu start mining
            if first_run:
                if claim(headers):
                    start_mining(headers)
                    time.sleep(2)
                else:
                    start_mining(headers)
                    time.sleep(1)

            # Coba sinkron lastClaimTime dari dashboard untuk akurasi
            lct_ms = fetch_dashboard_last_claim_ms(ck)

            if lct_ms is None:
                # Tidak bisa baca lastClaimTime → fallback ke state lokal atau coba start
                state = load_state()
                last_start_iso = state.get("last_start")
                if last_start_iso:
                    last_start_dt = parse_iso(last_start_iso)
                    next_claim_at = last_start_dt + timedelta(hours=TIMEDELTA_MINING_HOURS)
                else:
                    # Coba start untuk akun ini (kalau sudah aktif, handler akan set estimasi)
                    log(f"[{label}] Sinkron: mencoba start mining…")
                    start_mining(headers)
                    state = load_state()
                    if "last_start" in state:
                        next_claim_at = parse_iso(state["last_start"]) + timedelta(hours=TIMEDELTA_MINING_HOURS)
                    else:
                        # Tidak ada info sama sekali, lewati akun ini sementara
                        log(f"[{label}] Belum mendapat anchor waktu. Lewati sementara.")
                        continue
            else:
                # Gunakan waktu server + lastClaimTime dari server
                next_ms = lct_ms + 86_400_000
                next_claim_at = datetime.fromtimestamp(next_ms/1000, tz=timezone.utc)

            remaining = (next_claim_at - now_dt_loop).total_seconds()

            if remaining <= 0:
                log(f"[{label}] Waktu claim tiba → mencoba claim…")
                if claim(headers):
                    log(f"[{label}] Auto start mining baru…")
                    start_mining(headers)
                    # beri jeda singkat per akun
                    time.sleep(2)
                else:
                    time.sleep(2)
                # setelah tindakan, lanjut ke akun berikutnya
                continue

            # Laporkan countdown ringkas per akun
            total_sec = int(remaining)
            # Jika live countdown aktif (single atau multi), jangan spam log per akun di sini.
            if not (single_mode or MULTI_LIVE):
                log(f"[{label}] Claim dalam { _format_hms(total_sec) }")

            # Track paling cepat selesai untuk penjadwalan tidur global
            if earliest_ms is None:
                earliest_ms = int(now_dt_loop.timestamp() * 1000) + total_sec * 1000
            else:
                cand = int(now_dt_loop.timestamp() * 1000) + total_sec * 1000
                if cand < earliest_ms:
                    earliest_ms = cand
            targets.append((label, next_claim_at))

        # Tentukan tidur hingga mendekati akun terdekat (maks antara 60s dan NEARLY_DUE)
        if earliest_ms is None:
            time.sleep(CHECK_INTERVAL_MINUTES * 60)
        else:
            if single_mode or MULTI_LIVE:
                # Live tampilan gabungan hingga akun tercepat jatuh tempo
                _multi_countdown_until(targets, base_server_ms=srv_ms_loop)
                # Lanjut loop untuk eksekusi claim/start pada akun yang jatuh tempo
                continue
            # Non-live: tidur hingga mendekati yang paling cepat
            ahead_ms = max(0, earliest_ms - _server_now_ms())
            sleep_s = max(5, min(ahead_ms / 1000, CHECK_INTERVAL_MINUTES * 60))
            time.sleep(sleep_s)
        # Setelah satu putaran penuh, matikan mode percobaan awal
        if first_run:
            first_run = False


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Dihentikan oleh user.")
