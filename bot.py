import os, time, hashlib, sqlite3, logging, feedparser, tweepy
from anthropic import Anthropic
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

TWITTER_API_KEY      = os.environ["TWITTER_API_KEY"]
TWITTER_API_SECRET   = os.environ["TWITTER_API_SECRET"]
TWITTER_ACCESS_TOKEN = os.environ["TWITTER_ACCESS_TOKEN"]
TWITTER_ACCESS_SECRET= os.environ["TWITTER_ACCESS_SECRET"]
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]

PEAK_WINDOWS   = [(7,9,3),(11,13,3),(16,17,2),(20,22,4),(22,23,2)]
MIN_INTERVAL   = 25
DAILY_LIMIT    = 14
CHECK_INTERVAL = 600
SELF_REPLY_DELAY = 360

RSS_FEEDS = [
    {"url":"https://www.ntv.com.tr/son-dakika.rss",       "source":"NTV"},
    {"url":"https://www.hurriyet.com.tr/rss/anasayfa",    "source":"Hurriyet"},
    {"url":"https://www.sabah.com.tr/rss",                "source":"Sabah"},
    {"url":"https://www.cnnturk.com/feed/rss/all/news",   "source":"CNN Turk"},
    {"url":"https://www.bbc.com/turkce/index.xml",        "source":"BBC Turkce"},
    {"url":"https://tr.euronews.com/rss",                 "source":"Euronews TR"},
]

def init_db():
    conn = sqlite3.connect("pulsetr.db")
    conn.execute("CREATE TABLE IF NOT EXISTS posted (hash TEXT PRIMARY KEY, title TEXT, posted_at TEXT, tweet_id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS daily_count (date TEXT PRIMARY KEY, count INTEGER DEFAULT 0, last_tweet_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS pending_replies (tweet_id TEXT PRIMARY KEY, url TEXT, reply_at TEXT)")
    conn.commit()
    return conn

def is_posted(conn, url):
    h = hashlib.md5(url.encode()).hexdigest()
    return conn.execute("SELECT 1 FROM posted WHERE hash=?", (h,)).fetchone() is not None

def mark_posted(conn, url, title, tweet_id=""):
    h = hashlib.md5(url.encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO posted VALUES(?,?,?,?)", (h, title, datetime.now(timezone.utc).isoformat(), tweet_id))
    conn.commit()

def get_daily_info(conn):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute("SELECT count, last_tweet_at FROM daily_count WHERE date=?", (today,)).fetchone()
    return (row[0], row[1]) if row else (0, None)

def update_daily(conn):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO daily_count(date,count,last_tweet_at) VALUES(?,1,?) ON CONFLICT(date) DO UPDATE SET count=count+1, last_tweet_at=?", (today,now,now))
    conn.commit()

def add_pending_reply(conn, tweet_id, url):
    reply_at = datetime.now(timezone.utc).timestamp() + SELF_REPLY_DELAY
    conn.execute("INSERT OR IGNORE INTO pending_replies VALUES(?,?,?)", (tweet_id, url, str(reply_at)))
    conn.commit()

def get_due_replies(conn):
    now = datetime.now(timezone.utc).timestamp()
    return conn.execute("SELECT tweet_id, url FROM pending_replies WHERE CAST(reply_at AS REAL) <= ?", (now,)).fetchall()

def delete_reply(conn, tweet_id):
    conn.execute("DELETE FROM pending_replies WHERE tweet_id=?", (tweet_id,))
    conn.commit()

def is_peak():
    h = (datetime.now(timezone.utc).hour + 3) % 24
    return any(s <= h <= e for s,e,_ in PEAK_WINDOWS)

def mins_since(last_at):
    if not last_at: return 999
    last = datetime.fromisoformat(last_at)
    return (datetime.now(timezone.utc) - last).total_seconds() / 60

def fetch_news():
    items = []
    for f in RSS_FEEDS:
        try:
            for e in feedparser.parse(f["url"]).entries[:5]:
                items.append({
                    "title": e.get("title","").strip(),
                    "url": e.get("link","").strip(),
                    "summary": e.get("summary","").strip()[:400],
                    "source": f["source"]
                })
        except Exception as ex:
            log.warning(f"RSS [{f['source']}]: {ex}")
    return items

def is_content_sufficient(title, summary):
    combined = (title + " " + summary).lower()
    # Muglak ve bos haber filtresi
    vague_phrases = [
        "detaylar icin", "detaylar için", "resmi kaynak", "takip edin",
        "guncellenecek", "güncellenecek", "aciklama bekleniyor", "açıklama bekleniyor",
        "bilgi gelecek", "haberleri takip", "daha fazla bilgi",
        "guncellenecektir", "güncellenecektir", "guncel bilgi", "güncel bilgi",
        "bilgiler guncel", "bilgiler güncellendi", "bilgileri guncel",
        "aciklanacak", "açıklanacak", "bekleyiniz"
    ]
    for phrase in vague_phrases:
        if phrase in combined:
            return False
    # En az somut bir bilgi olmali: sayi, yer, kisi adi, yuzde vb.
    import re
    has_number = bool(re.search(r'\d', combined))
    has_location = any(loc in combined for loc in ["istanbul","ankara","izmir","türkiye","turkey","avrupa","abd","rusya","çin","almanya","fransa","irak","suriye","iran"])
    has_person = any(title_word in combined for title_word in ["erdoğan","erdogan","bakan","başbakan","cumhurbaşkan","trump","putin","zelensky","macron"])
    if not (has_number or has_location or has_person):
        return False
    # Cok kisa ozet de atla
    if len(title) < 20:
        return False
    return True

def generate_tweet(client, title, summary, source):
    h = (datetime.now(timezone.utc).hour + 3) % 24

    if 7 <= h <= 9:
        fmt_inst = """Liste formati: her madde bullet ile baslasin, max 3 madde, somut veri/rakam kullan.
Son satirda: 'Kaydet ↗ takip et' yaz."""
    elif 11 <= h <= 13:
        fmt_inst = """Kisa somut bilgi ver (rakam, yer, kisi), sonra gercekten merak uyandiran acik uclu soru sor.
Max 200 karakter. Ornekler: 'Sizce bu nasil etkiler?', 'Bu karar dogru muydu?', 'Sizin bolgenizde nasil?'"""
    elif 16 <= h <= 17:
        fmt_inst = """Son dakika formati. Max 110 karakter. Sadece haber icerigi, somut bilgi.
Baslangica uygun 1 emoji koy."""
    else:
        fmt_inst = """Carpici somut bilgiyle basla. Yapici bir yorum veya baglam ekle. Soru ile bitir.
Max 230 karakter."""

    prompt = f"""Sen Turkce bir haber botu icin tweet yaziyorsun.

Haber basligi: {title}
Kaynak: {source}
Ozet: {summary}

FORMAT TALIMATI: {fmt_inst}

KESINLIKLE YASAKLAR (bu kurallari cignersen tweet KULLANICIYA ZARAR VERIR):
1. URL, link, http, www EKLEME - tweet icinde kesinlikle link olmayacak
2. "Detaylar icin takip edin", "Resmi kaynaklara bakin", "Bilgi guncellenecek" gibi BOSLUK DOLDURUCU ifadeler kullanma
3. Haberde somut bilgi (rakam/yer/kisi/olay) yoksa SADECE "YETERSIZ_HABER" yaz, baska hicbir sey yazma
4. "Like if / RT if" engagement bait kullanma
5. 3+ hashtag kullanma - max 1-2 nis hashtag
6. Negatif/kavgaci ton - Grok bunu penalize ediyor

IYI TWEET ORNEKLERI:
- "Merkez Bankasi politika faizini %42.5'te sabit tuttu. Enflasyonla mucadelede temkinli adim. Piyasalar bu karari nasil karsilayacak? #Ekonomi"
- "BIST100 bugun %2.3 yukselisle 9.840 puanda kapandi. Son 3 ayin en yuksek seviyesi. Kaydet ↗ #Borsa"
- "🔴 Bursa'da 4.2 buyuklugunde deprem. AFAD: can kaybi yok."

KOTU TWEET ORNEKLERI (bunlari yazma):
- "Deprem bilgileri guncellenmistir. Detaylar icin resmi kaynaklari takip edin." (BOS - somut bilgi yok)
- "Son dakika haberleri icin sayfamizi takip edin" (REKLAM degil haber yaz)
- "Bilgiler guncellenecektir" (ANLAMSIZ)

Sadece tweeti yaz. Haber yetersizse sadece YETERSIZ_HABER yaz."""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        messages=[{"role":"user","content":prompt}]
    )
    tweet = resp.content[0].text.strip()

    # Claude yetersiz dedi mi kontrol et
    if "YETERSIZ_HABER" in tweet or len(tweet) < 30:
        return None

    # Link var mi kontrol et
    import re
    if re.search(r'https?://|www\.', tweet):
        log.warning("Claude link ekledi, tweet reddedildi.")
        return None

    if len(tweet) > 270:
        tweet = tweet[:267] + "..."

    return tweet

def post_tweet(twitter, text):
    try:
        r = twitter.create_tweet(text=text)
        log.info(f"Tweet: {text[:70]}...")
        return r.data["id"]
    except tweepy.TweepyException as e:
        log.error(f"Tweet error: {e}")
        return None

def post_reply(twitter, tweet_id, text):
    try:
        twitter.create_tweet(text=text, in_reply_to_tweet_id=tweet_id)
        log.info(f"Reply -> {tweet_id}")
        return True
    except tweepy.TweepyException as e:
        log.error(f"Reply error: {e}")
        return False

def run():
    log.info("PulseTR Bot v3.1 started.")
    conn = init_db()
    anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
    twitter = tweepy.Client(
        consumer_key=TWITTER_API_KEY, consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN, access_token_secret=TWITTER_ACCESS_SECRET
    )

    while True:
        try:
            # Bekleyen self-reply'leri gonder
            for tid, url in get_due_replies(conn):
                post_reply(twitter, tid, f"Kaynak: {url}")
                delete_reply(conn, tid)
                time.sleep(5)

            count, last_at = get_daily_info(conn)

            if count >= DAILY_LIMIT:
                log.info(f"Daily limit ({DAILY_LIMIT}). Sleeping...")
                time.sleep(CHECK_INTERVAL)
                continue

            if not is_peak():
                h = (datetime.now(timezone.utc).hour + 3) % 24
                log.info(f"Off-peak ({h}:xx TR). Sleeping...")
                time.sleep(CHECK_INTERVAL)
                continue

            m = mins_since(last_at)
            if m < MIN_INTERVAL:
                wait = int((MIN_INTERVAL - m) * 60)
                log.info(f"Interval {m:.1f}/{MIN_INTERVAL}min. Wait {wait}s...")
                time.sleep(min(wait, CHECK_INTERVAL))
                continue

            news = fetch_news()
            log.info(f"Fetched {len(news)} items.")
            posted = False

            for item in news:
                if not item["url"] or not item["title"]: continue
                if is_posted(conn, item["url"]): continue

                # Icerik kalite filtresi
                if not is_content_sufficient(item["title"], item["summary"]):
                    log.info(f"SKIP (vague): {item['title'][:60]}")
                    mark_posted(conn, item["url"], item["title"])  # tekrar denemesin
                    continue

                tweet_text = generate_tweet(anthropic, item["title"], item["summary"], item["source"])

                if tweet_text is None:
                    log.info(f"SKIP (insufficient by Claude): {item['title'][:60]}")
                    mark_posted(conn, item["url"], item["title"])
                    continue

                tid = post_tweet(twitter, tweet_text)
                if tid:
                    mark_posted(conn, item["url"], item["title"], tid)
                    update_daily(conn)
                    add_pending_reply(conn, tid, item["url"])
                    posted = True
                    break

            if not posted:
                log.info("No suitable news this round.")

            c2, _ = get_daily_info(conn)
            log.info(f"Daily: {c2}/{DAILY_LIMIT}. Next {CHECK_INTERVAL//60}min.")
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run()
