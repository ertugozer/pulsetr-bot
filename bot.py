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

# Peak saatler GMT+3 (baslangic, bitis, pencere basi tweet limiti)
PEAK_WINDOWS   = [(7,9,3),(11,13,3),(16,17,2),(20,22,4),(22,23,2)]
MIN_INTERVAL   = 25   # dakika - tweetler arasi minimum sure
DAILY_LIMIT    = 14   # gunluk max (X API maliyeti ~$0.14)
CHECK_INTERVAL = 600  # 10 dakikada bir kontrol
SELF_REPLY_DELAY = 360 # kendi reply'ini 6 dakika sonra at

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
    rows = conn.execute("SELECT tweet_id, url FROM pending_replies WHERE CAST(reply_at AS REAL) <= ?", (now,)).fetchall()
    return rows

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
                items.append({"title":e.get("title","").strip(),"url":e.get("link","").strip(),"summary":e.get("summary","").strip()[:400],"source":f["source"]})
        except Exception as ex:
            log.warning(f"RSS [{f['source']}]: {ex}")
    return items

def generate_tweet(client, title, summary, source):
    h = (datetime.now(timezone.utc).hour + 3) % 24

    # Formati saate gore sec:
    # 07-09: sabah ozeti (liste + kaydet)
    # 11-13: veri + soru (reply tetikler)
    # 16-17: son dakika kisa
    # 20-23: analiz + soru (gece tartisma)
    if 7 <= h <= 9:
        fmt_inst = """Liste formati kullan. Her madde • ile baslasin, max 3 madde.
Son satirda: 'Kaydet ↗ takip et' yaz. (Bookmark talep etmek ×10 guc veriyor)
Max 240 karakter."""
    elif 11 <= h <= 13:
        fmt_inst = """Kisa bir veri/istatistik ver, sonra gercekten merak uyandiran bir soru sor.
Soru acik uclu olmali (evet/hayir degil). Turkiye'den insanlarin cevap vermek isteyecegi tur.
Max 200 karakter. Soru reply getiriyor (×13.5 guc)."""
    elif 16 <= h <= 17:
        fmt_inst = """Son dakika formati. Cok kisa, max 100 karakter. Sadece onemli olan.
Emoji ile basla (kirmizi daire veya diger uygun emoji kullanabilirsin ama sadece 1 tane)."""
    else:  # 20-23 gece
        fmt_inst = """Carpici bir gercekle basla. Sonra tartismaya acik bir yorum ekle.
Son satirda soru sor veya 'Ne dusunuyorsunuz?' yaz. Max 230 karakter.
Gece saatlerinde tartisma tweetleri viral oluyor."""

    prompt = f"""Turkce haber tweeti yaz. LINK EKLEME - kesinlikle url veya http icermesin.

Haber: {title}
Kaynak: {source}
Ozet: {summary}

Format talimati: {fmt_inst}

Ek kurallar:
- Max 1-2 hashtag, nise ozel (orn: #Ekonomi, #Deprem, #Teknoloji - genel #SonDakika KULLANMA)
- Pozitif ve yapici ton (Grok negatif icerigi kisitiyor)
- "Like if / RT if" gibi ifadeler KULLANMA (spam olarak isaretleniyor)
- Sadece tweeti yaz, baska hicbir sey ekleme"""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        messages=[{"role":"user","content":prompt}]
    )
    tweet = resp.content[0].text.strip()
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
        log.info(f"Reply posted to {tweet_id}")
        return True
    except tweepy.TweepyException as e:
        log.error(f"Reply error: {e}")
        return False

def run():
    log.info("PulseTR Bot v3 started.")
    conn = init_db()
    anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
    twitter = tweepy.Client(
        consumer_key=TWITTER_API_KEY, consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN, access_token_secret=TWITTER_ACCESS_SECRET
    )

    while True:
        try:
            # Bekleyen kendi reply'larini gonder (×150 algoritma gucu)
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
                log.info(f"Interval: {m:.1f}/{MIN_INTERVAL} min. Wait {wait}s...")
                time.sleep(min(wait, CHECK_INTERVAL))
                continue

            news = fetch_news()
            log.info(f"Fetched {len(news)} items.")

            for item in news:
                if not item["url"] or not item["title"]: continue
                if is_posted(conn, item["url"]): continue

                tweet_text = generate_tweet(anthropic, item["title"], item["summary"], item["source"])
                tid = post_tweet(twitter, tweet_text)

                if tid:
                    mark_posted(conn, item["url"], item["title"], tid)
                    update_daily(conn)
                    # 6 dk sonra kaynak linkini kendi reply olarak at (×150 boost)
                    add_pending_reply(conn, tid, item["url"])
                    break

            count2, _ = get_daily_info(conn)
            log.info(f"Daily: {count2}/{DAILY_LIMIT}. Next check {CHECK_INTERVAL//60}min.")
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run()
