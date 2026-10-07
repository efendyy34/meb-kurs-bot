#!/usr/bin/env python3
"""MEB e-Yaygın "Açık Kurslar" sayfasını izleyip yeni kurs açıldığında Telegram'a bildirir.

Kullanım:
  python3 bot.py --test-telegram   # Telegram bağlantısını dener
  python3 bot.py --discover        # Sayfadaki alanları bulup bulamadığını kontrol eder
  python3 bot.py --dump            # Sayfa yapısını sayfa.html olarak kaydeder (sorun giderme)
  python3 bot.py                   # Bir kez kontrol eder (cron / Görev Zamanlayıcı için)
  python3 bot.py --loop 15         # 15 dakikada bir sürekli kontrol eder
"""
import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://e-yaygin.meb.gov.tr/pageKurslar.aspx"
BASE = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", BASE / "config.json"))
STATE_FILE = Path(os.environ.get("STATE_FILE", BASE / "seen.json"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_PAGES = 40

# Sayfadaki (Telerik) kontrol kimlikleri
IDS = {"il": "cmbKursIlKodu", "ilce": "cmbKursIlceKodu", "kurs": "txtKursAdi", "btn": "btnSearch"}
DEFAULT_HEADERS = ["", "Kurs No", "Kurs Adı", "İl", "İlçe", "Kurum", "Eğitim Şekli",
                   "Kursun Yapılacağı Yer", "Baş.Tarihi", "Bit.Tarihi", "Süre", "Kontenjan",
                   "Ders Planı", "Şartlar"]


# ----------------------------------------------------------------- yardımcılar
def fold(s):
    """Türkçe karakterleri ve büyük/küçük harf farkını yok sayarak karşılaştırma anahtarı üretir."""
    s = (s or "").replace("İ", "i").replace("I", "ı").lower()
    return re.sub(r"\s+", " ", s.translate(str.maketrans("çğıöşüâîû", "cgiosuaiu"))).strip()


def load_config():
    cfg = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg["telegram_token"] = os.environ.get("TELEGRAM_TOKEN", cfg.get("telegram_token", ""))
    cfg["telegram_chat_id"] = str(os.environ.get("TELEGRAM_CHAT_ID", cfg.get("telegram_chat_id", "")))
    cfg.setdefault("watches", [{"il": "", "ilce": "", "kurs_adi": ""}])
    return cfg


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ------------------------------------------------------------------- Telegram
def telegram_send(cfg, text):
    if not cfg["telegram_token"] or not cfg["telegram_chat_id"]:
        sys.exit("telegram_token / telegram_chat_id ayarlı değil (config.json veya ortam değişkeni).")
    # 4096 karakter sınırı için parçala
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage",
            data={"chat_id": cfg["telegram_chat_id"], "text": c,
                  "parse_mode": "HTML", "disable_web_page_preview": "true"},
            timeout=30,
        )
        if not r.ok:
            raise RuntimeError(f"Telegram hatası {r.status_code}: {r.text[:200]}")


# ------------------------------------------------------- ASP.NET form işlemleri
def collect(soup):
    """Formdaki tüm alanları (gizli VIEWSTATE dahil) postback için sözlüğe çevirir."""
    form = soup.find("form") or soup
    data = {}
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name:
            continue
        if el.name == "input":
            t = (el.get("type") or "text").lower()
            if t in ("submit", "button", "image", "reset", "file"):
                continue
            if t in ("checkbox", "radio"):
                if el.has_attr("checked"):
                    data[name] = el.get("value", "on")
                continue
            data[name] = el.get("value", "")
        elif el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            data[name] = (opt.get("value", opt.get_text()) if opt else "")
        else:
            data[name] = el.get_text()
    return data


def find_controls(soup):
    """Sayfadaki il/ilçe kutuları, kurs adı alanı ve arama düğmesi var mı kontrol eder."""
    missing = [v for v in IDS.values() if soup.find(id=v) is None]
    if missing:
        raise RuntimeError("Sayfada beklenen alanlar yok: " + ", ".join(missing)
                           + ". Sayfa yapısı değişmiş olabilir; 'python3 bot.py --dump' çıktısını inceleyin.")
    btn = soup.find(id=IDS["btn"])
    return dict(IDS, btn_value=btn.get("value") or "Kursları Listele")


def combo_items(soup, cid):
    """Telerik RadComboBox öğelerini [(metin, değer)] olarak döndürür."""
    box = soup.find("div", id=cid)
    if box is None:
        return []
    texts = [li.get_text(strip=True) for li in box.find_all("li", class_=re.compile("rcbItem"))]
    vals = []
    for sc in soup.find_all("script"):
        s = sc.string or sc.get_text() or ""
        k = s.find('"_uniqueId":"%s"' % cid)
        if k < 0:
            continue
        j = s.find('"itemData":', k)
        if j >= 0:
            vals = re.findall(r'"value":"([^"]*)"', s[j:s.find("]", j)])
        break
    if len(vals) != len(texts):
        vals = texts
    return list(zip(texts, vals))


def pick_combo(soup, cid, wanted):
    """İstenen metne uyan öğeyi (sıra, metin, değer) olarak döndürür."""
    items = combo_items(soup, cid)
    w = fold(wanted)
    for i, (t, v) in enumerate(items):
        if fold(t) == w:
            return i, t, v
    for i, (t, v) in enumerate(items):
        if w and w in fold(t) and v != "-1":
            return i, t, v
    raise ValueError(f"'{wanted}' bulunamadı. Mevcut seçenekler: "
                     + ", ".join(t for t, _ in items[:90]))


def set_combo(payload, cid, text, value):
    payload[cid] = text
    payload[cid + "_ClientState"] = json.dumps(
        {"logEntries": [], "value": value, "text": text, "enabled": True,
         "checkedIndices": [], "checkedItemsTextOverflows": False},
        ensure_ascii=False, separators=(",", ":"))


def set_textbox(payload, tid, text):
    payload[tid] = text
    payload[tid + "_ClientState"] = json.dumps(
        {"enabled": True, "emptyMessage": "", "validationText": text,
         "valueAsString": text, "lastSetTextBoxValue": text},
        ensure_ascii=False, separators=(",", ":"))


def parse_grid(soup):
    """Sonuç tablosunu {kurs_no: {sütun: değer}} olarak döndürür. (rows, bos_mu)
    Telerik tablosunda başlık ve satırlar ayrı iki tablodadır."""
    empty = soup.find("tr", class_="rgNoRecords") is not None
    hdr = soup.find("table", id="rgKurslar_ctl00_Header")
    headers = [th.get_text(" ", strip=True) for th in hdr.find_all("th")] if hdr else []
    if "Kurs No" not in headers:
        headers = DEFAULT_HEADERS
    body = soup.find("table", id="rgKurslar_ctl00")
    rows = {}
    if body is not None:
        for tr in body.find_all("tr", class_=re.compile(r"rgRow|rgAltRow")):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
            if len(cells) > len(headers):
                cells = cells[-len(headers):]
            row = {h: c for h, c in zip(headers, cells) if h}
            kno = row.get("Kurs No", "").strip()
            if kno:
                rows[kno] = row
    return rows, empty


def next_page(soup):
    """Sayfalama çubuğunda bir sonraki sayfanın (__EVENTTARGET, __EVENTARGUMENT) değerini döndürür."""
    pager = soup.find(id="rgKurslar_ctl00_Pager") or soup.find(class_=re.compile("rgPager"))
    if not pager:
        return None
    cur = pager.find(class_=re.compile("rgCurrentPage"))
    try:
        n = int(cur.get_text(strip=True))
    except (AttributeError, ValueError):
        n = 1
    pat = re.compile(r"__doPostBack\('([^']*)','([^']*)'\)")
    for a in pager.find_all("a"):
        if a.get_text(strip=True) == str(n + 1):
            m = pat.search(a.get("href", ""))
            if m:
                return m.groups()
    seen_cur = False
    for el in pager.find_all(["a", "span"]):
        if cur is not None and (el is cur or cur in el.parents or el in cur.parents):
            seen_cur = True
            continue
        if seen_cur and el.name == "a" and el.get_text(strip=True) == "...":
            m = pat.search(el.get("href", ""))
            if m:
                return m.groups()
    return None


# ---------------------------------------------------------------- sorgulama
class Site:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.s.headers["Accept-Language"] = "tr-TR,tr;q=0.9"

    def get(self):
        r = self.s.get(URL, timeout=40)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")

    def post(self, payload):
        r = self.s.post(URL, data=payload, timeout=60, headers={"Referer": URL})
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")


def ilce_cascade(site, c, payload, il_pick):
    """İl seçimini sunucuya bildirip ilçe listesi dolu sayfayı döndürür (olmazsa None)."""
    idx = il_pick[0] if il_pick else 0
    for arg in ("", '{"Command":"Select","Index":%d}' % idx, "arguments"):
        p = dict(payload)
        p.update({"__EVENTTARGET": c["il"], "__EVENTARGUMENT": arg})
        s2 = site.post(p)
        if combo_items(s2, c["ilce"]):
            return s2
    return None


def fetch_ilceler(site, il):
    """Bir ilin ilçe adlarını sitenin kendi listesinden çeker (alınamazsa boş liste)."""
    soup = site.get()
    c = find_controls(soup)
    payload = collect(soup)
    pick = pick_combo(soup, c["il"], il)
    set_combo(payload, c["il"], pick[1], pick[2])
    s2 = ilce_cascade(site, c, payload, pick)
    if s2 is None:
        return []
    return [t for t, v in combo_items(s2, c["ilce"]) if v not in ("-1", "") and not t.startswith("--")]


def fetch_il_listesi(site):
    """Sitedeki il adlarını döndürür."""
    soup = site.get()
    c = find_controls(soup)
    return [t for t, v in combo_items(soup, c["il"]) if v != "-1" and not t.startswith("--")]


def search(site, watch):
    soup = site.get()
    c = find_controls(soup)
    il, ilce, ad = watch.get("il", ""), watch.get("ilce", ""), watch.get("kurs_adi", "")

    payload = collect(soup)
    il_pick = None
    if il:
        il_pick = pick_combo(soup, c["il"], il)
        set_combo(payload, c["il"], il_pick[1], il_pick[2])

    if ilce:
        # İlçe listesi il'e bağlı: il seçimini sunucuya bildirip ilçe listesini yükle
        loaded = ilce_cascade(site, c, payload, il_pick)
        if loaded is None:
            raise RuntimeError("İlçe listesi yüklenemedi. İl seçimi gerekli; "
                               "config.json'da 'il' alanını doldurduğunuzdan emin olun.")
        soup = loaded
        payload = collect(soup)
        if il_pick:
            set_combo(payload, c["il"], il_pick[1], il_pick[2])
        _, t, v = pick_combo(soup, c["ilce"], ilce)
        set_combo(payload, c["ilce"], t, v)

    set_textbox(payload, c["kurs"], ad)
    payload[c["btn"]] = c["btn_value"]
    payload.update({"__EVENTTARGET": "", "__EVENTARGUMENT": ""})
    soup = site.post(payload)

    results = {}
    for _ in range(MAX_PAGES):
        rows, empty = parse_grid(soup)
        if not rows and not empty and not results:
            raise RuntimeError("Sonuç tablosu okunamadı (sayfa yapısı değişmiş olabilir).")
        new_here = [k for k in rows if k not in results]
        results.update(rows)
        nxt = next_page(soup)
        if not nxt or not new_here:
            break
        payload = collect(soup)
        payload.update({"__EVENTTARGET": nxt[0], "__EVENTARGUMENT": nxt[1]})
        soup = site.post(payload)
        time.sleep(0.5)
    else:
        print(f"UYARI: {MAX_PAGES} sayfadan fazla sonuç var, liste kısaltıldı. "
              f"Daha dar bir kurs adı kullanın.", file=sys.stderr)
    return results


def free_slots(row):
    """'50/50' gibi kontenjan metninden boş yer sayısını çıkarır (okunamazsa None).
    Kayıtlı sayı toplamı geçmeyeceği için iki sayının farkı, sıra ne olursa olsun boş yeri verir."""
    m = re.search(r"(\d+)\s*/\s*(\d+)", row.get("Kontenjan", "") or "")
    return abs(int(m.group(1)) - int(m.group(2))) if m else None


def format_course(row, title="🆕"):
    g = lambda k: html.escape(row.get(k, "") or "-")
    fs = free_slots(row)
    slots = "bilinmiyor" if fs is None else ("DOLU" if fs == 0 else f"<b>{fs} boş yer</b>")
    return (f"{title} <b>{g('Kurs Adı')}</b>\n"
            f"📍 {g('İl')} / {g('İlçe')}\n"
            f"🏫 {g('Kurum')}\n"
            f"🏠 Yer: {g('Kursun Yapılacağı Yer')}\n"
            f"📅 {g('Baş.Tarihi')} – {g('Bit.Tarihi')}  ({g('Süre')})\n"
            f"👥 Kontenjan: {g('Kontenjan')} → {slots}  |  Şekil: {g('Eğitim Şekli')}\n"
            f"Kurs No: <code>{g('Kurs No')}</code>\n")


def normalize_watch(w):
    """Eski ({il, ilce, kurs_adi}) ve yeni ({il, ilceler, kurs_adlari, anahtar}) izleme biçimlerini birleştirir."""
    ilceler = list(w.get("ilceler") or ([w["ilce"]] if w.get("ilce") else []))
    return {"il": w.get("il", "") or "", "ilceler": ilceler,
            "kurs_adlari": list(w.get("kurs_adlari") or []),
            "anahtar": (w.get("anahtar", w.get("kurs_adi", "")) or "")}


def watch_label(w):
    w = normalize_watch(w)
    ilce = ""
    if w["ilceler"]:
        ilce = ", ".join(w["ilceler"]) if len(w["ilceler"]) <= 3 else f"{len(w['ilceler'])} ilçe"
    kurs = ", ".join(w["kurs_adlari"]) if w["kurs_adlari"] else w["anahtar"]
    return " / ".join(x for x in (w["il"], ilce, kurs) if x) or "Tüm kurslar"


def watch_key(w):
    w = normalize_watch(w)
    parts = [fold(w["il"]), ",".join(sorted(fold(i) for i in w["ilceler"])), fold(w["anahtar"])]
    if w["kurs_adlari"]:
        parts.append(",".join(sorted(fold(n) for n in w["kurs_adlari"])))
    return "|".join(parts)


def fetch_watch(site, w):
    """Bir izlemenin kurslarını getirir. İlçe ve tam kurs adı süzmesi burada (istemci tarafında) yapılır."""
    w = normalize_watch(w)
    queries = w["kurs_adlari"] or [w["anahtar"]]
    names = {fold(n) for n in w["kurs_adlari"]}
    ilc = {fold(i) for i in w["ilceler"]}
    out = {}
    for q in queries:
        for kno, row in search(site, {"il": w["il"], "ilce": "", "kurs_adi": q}).items():
            if ilc and fold(row.get("İlçe", "")) not in ilc:
                continue
            if names and fold(row.get("Kurs Adı", "")) not in names:
                continue
            out[kno] = row
    return out


def check_once(cfg, notify_existing=False):
    """Tüm izlemeleri bir kez tarar; başarısız izleme sayısını döndürür."""
    state = load_state()
    site = Site()
    errors = 0
    for w in cfg["watches"]:
        key = watch_key(w)
        label = watch_label(w)
        try:
            found = fetch_watch(site, w)
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] HATA: {e}", file=sys.stderr)
            errors += 1
            continue
        prev = state.get(key)
        first_run = prev is None
        if isinstance(prev, list):            # eski kayıt biçimi
            prev = {k: None for k in prev}
        prev = prev or {}
        
        bildirim_filtresi = w.get("bildirim_kurslari", [])
        def can_notify(row):
            if not bildirim_filtresi: return True
            return any(fold(b) in fold(row.get("Kurs Adı", "")) for b in bildirim_filtresi)

        new = {k: v for k, v in found.items() if k not in prev and can_notify(v)}
        # daha önce dolu (0 boş yer) görünen kursta şimdi yer varsa
        opened = {k: v for k, v in found.items()
                  if k in prev and prev[k] == 0 and (free_slots(v) or 0) > 0 and can_notify(v)}
                  
        print(f"[{label}] {len(found)} kurs, {len(new)} yeni, {len(opened)} yerde boşluk açıldı")
        if first_run and not notify_existing:
            full = sum(1 for v in found.values() if free_slots(v) == 0)
            telegram_send(cfg, f"✅ Bot başladı: <b>{html.escape(label)}</b> için {len(found)} mevcut kurs "
                               f"kaydedildi ({full} tanesi dolu). Bundan sonra yeni açılan kurslar ve "
                               f"dolu kurslarda açılan yerler bildirilecek.")
        else:
            parts = []
            if new:
                parts.append(f"🔔 <b>{html.escape(label)}</b>: {len(new)} yeni kurs\n\n"
                             + "\n".join(format_course(v) for v in new.values()))
            if opened:
                parts.append(f"🔓 <b>{html.escape(label)}</b>: {len(opened)} kursta yer açıldı\n\n"
                             + "\n".join(format_course(v, "🔓") for v in opened.values()))
            if parts:
                telegram_send(cfg, "\n".join(parts))
        # kaybolan kursları da tut ki geri gelirse "yeni" sayılmasın
        merged = dict(prev)
        merged.update({k: free_slots(v) for k, v in found.items()})
        state[key] = merged
        save_state(state)
    return errors


def discover():
    soup = Site().get()
    c = find_controls(soup)
    print("Alanlar bulundu:", ", ".join(f"{k}={c[k]}" for k in IDS))
    il = combo_items(soup, c["il"])
    print(f"\nİl listesi: {len(il)} öğe. İlk 6: " + ", ".join(f"{t}={v}" for t, v in il[:6]))
    try:
        i, t, v = pick_combo(soup, c["il"], "İstanbul")
        print(f"'İstanbul' → '{t}' (kod {v})")
    except ValueError as e:
        print("İstanbul bulunamadı:", e)
    print("\nGizli alanlar:", ", ".join(k for k in collect(soup) if k.startswith("__")))


def dump():
    """Sayfanın HTML'ini (VIEWSTATE gibi dev alanlar kısaltılmış) sayfa.html olarak kaydeder
    ve formdaki tüm kontrolleri listeler. Dosyada kişisel bilgi yoktur."""
    site = Site()
    soup = site.get()
    for el in soup.find_all("input"):
        if (el.get("name") or "").startswith("__") and el.get("value"):
            el["value"] = "..."
    out = BASE / "sayfa.html"
    out.write_text(str(soup), encoding="utf-8")
    print(f"Kaydedildi: {out}  ({out.stat().st_size // 1024} KB)\n")
    print("Form kontrolleri:")
    for el in soup.find_all(["input", "select", "textarea", "button"]):
        n = el.get("name") or el.get("id") or ""
        if n.startswith("__") or "rgKurslar" in n:
            continue
        print(f"  <{el.name}> type={el.get('type')} name={el.get('name')} id={el.get('id')} "
              f"value={(el.get('value') or el.get_text(strip=True))[:40]!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="Sayfa yapısını sayfa.html olarak kaydet")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--loop", type=int, metavar="DAKİKA")
    ap.add_argument("--notify-existing", action="store_true",
                    help="İlk çalıştırmada mevcut kursları da bildir")
    a = ap.parse_args()
    cfg = load_config()
    if a.dump:
        return dump()
    if a.discover:
        return discover()
    if a.test_telegram:
        telegram_send(cfg, "✅ Telegram bağlantısı çalışıyor.")
        return print("Test mesajı gönderildi.")
    if a.loop:
        while True:
            try:
                check_once(cfg, a.notify_existing)
            except Exception as e:  # noqa: BLE001
                print("Döngü hatası:", e, file=sys.stderr)
            time.sleep(a.loop * 60)
    else:
        check_once(cfg, a.notify_existing)


if __name__ == "__main__":
    main()
