import os, time, hashlib, sqlite3, logging, feedparser, tweepy, re
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
    {"url":"https://www.ntv.com.tr/son-dakika.rss",     "source":"NTV"},
    {"url":"https://www.hurriyet.com.tr/rss/anasayfa",  "source":"Hurriyet"},
    {"url":"https://www.sabah.com.tr/rss",              "source":"Sabah"},
    {"url":"https://www.cnnturk.com/feed/rss/all/news", "source":"CNN Turk"},
    {"url":"https://www.bbc.com/turkce/index.xml",      "source":"BBC Turkce"},
    {"url":"https://tr.euronews.com/rss",               "source":"Euronews TR"},
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
    return (datetime.now(timezone.utc) - datetime.fromisoformat(last_at)).total_seconds() / 60

def fetch_news():
    items = []
    for f in RSS_FEEDS:
        try:
            for e in feedparser.parse(f["url"]).entries[:5]:
                items.append({"title":e.get("title","").strip(),"url":e.get("link","").strip(),"summary":e.get("summary","").strip()[:400],"source":f["source"]})
        except Exception as ex:
            log.warning(f"RSS [{f['source']}]: {ex}")
    return items

def is_content_sufficient(title, summary):
    combined = (title + " " + summary).lower()
    vague = ["detaylar icin","detaylar icin","resmi kaynak","takip edin","guncellenecek",
             "guncellenecek","aciklama bekleniyor","bilgi gelecek","bilgileri guncel",
             "aciklanacak","bekleyiniz"]
    if any(p in combined for p in vague): return False
    has_num = bool(re.search(r'\d', combined))
    has_loc = any(l in combined for l in ["istanbul","ankara","izmir","turkiye","avrupa","abd","rusya","cin","almanya","israil","irak","suriye"])
    has_who = any(p in combined for p in ["erdogan","bakan","cumhurbaskani","trump","putin","zelensky","netanyahu"])
    if not (has_num or has_loc or has_who): return False
    return len(title) >= 20

def fix_hashtags(tweet):
    # "#Son Dakika" -> "#SonDakika" gibi kirik hashtag onarimi
    tweet = re.sub(r'#([A-Za-z\u00C0-\u017E]+)\s+([A-Za-z\u00C0-\u017E]+)', lambda m: '#' + m.group(1) + m.group(2), tweet)
    return tweet

def validate_tweet(tweet):
    if not tweet or len(tweet) < 30: return None
    if "YETERSIZ_HABER" in tweet: return None
    if re.search(r'https?://|www\.', tweet):
        log.warning("Link var, reddedildi.")
        return None
    tweet = fix_hashtags(tweet)
    tags = re.findall(r'#(\S+)', tweet)
    if len(tags) > 2:
        for extra in tags[2:]:
            tweet = tweet.replace(f"#{extra}", "").strip()
    return tweet[:270].strip() if len(tweet) > 270 else tweet.strip()

def generate_tweet(client, title, summary, source):
    h = (datetime.now(timezone.utc).hour + 3) % 24
    if 7 <= h <= 9:
        fmt = "Liste: • ile 3 madde, somut rakam/bilgi. Son satir: 'Kaydet takip et'"
    elif 11 <= h <= 13:
        fmt = "Somut bilgi + acik uclu soru. Max 200 karakter. Ornek: 'Sizce bu nasil etkiler?'"
    elif 16 <= h <= 17:
        fmt = "Son dakika, max 110 karakter, 1 emoji baslangica, somut bilgi."
    else:
        fmt = "Carpici bilgi + yapici yorum + soru. Max 230 karakter."

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        messages=[{"role":"user","content":f"""Turkce haber tweeti yaz.

Haber: {title}
Ozet: {summary}

Format: {fmt}

KURALLAR:
- URL/link EKLEME
- Bos ifade yasak: 'takip edin', 'resmi kaynaktan bakin' yazma
- Yetersiz haberse sadece: YETERSIZ_HABER
- HASHTAG: tek kelime, bosluksuz. DOGRU: #SonDakika #Ekonomi  YANLIS: #Son Dakika
- Max 2 hashtag, spesifik nis tag kullan
- Dogrudan uzun alinti yapma, ozetle
- Pozitif ton

Sadece tweeti yaz:"""}]
    )
    return validate_tweet(resp.content[0].text.strip())

def post_tweet(twitter, text):
    try:
        r = twitter.create_tweet(text=text)
        log.info(f"Tweet: {text[:70]}")
        return r.data["id"]
    except tweepy.TweepyException as e:
        log.error(f"Tweet error: {e}")
        return None

def post_reply(twitter, tweet_id, text):
    try:
        twitter.create_tweet(text=text, in_reply_to_tweet_id=tweet_id)
        log.info(f"Reply -> {tweet_id}")
    except tweepy.TweepyException as e:
        log.error(f"Reply error: {e}")

def run():
    log.info("PulseTR Bot v3.2 started.")
    conn = init_db()
    anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
    twitter = tweepy.Client(consumer_key=TWITTER_API_KEY, consumer_secret=TWITTER_API_SECRET,
                            access_token=TWITTER_ACCESS_TOKEN, access_token_secret=TWITTER_ACCESS_SECRET)
    while True:
        try:
            for tid, url in get_due_replies(conn):
                post_reply(twitter, tid, f"Kaynak: {url}")
                delete_reply(conn, tid)
                time.sleep(5)

            count, last_at = get_daily_info(conn)
            if count >= DAILY_LIMIT:
                log.info("Daily limit. Sleeping...")
                time.sleep(CHECK_INTERVAL); continue

            if not is_peak():
                h = (datetime.now(timezone.utc).hour + 3) % 24
                log.info(f"Off-peak ({h}:xx). Sleeping...")
                time.sleep(CHECK_INTERVAL); continue

            m = mins_since(last_at)
            if m < MIN_INTERVAL:
                wait = int((MIN_INTERVAL - m) * 60)
                log.info(f"Interval {m:.1f}/{MIN_INTERVAL}min. Wait {wait}s...")
                time.sleep(min(wait, CHECK_INTERVAL)); continue

            news = fetch_news()
            posted = False
            for item in news:
                if not item["url"] or not item["title"]: continue
                if is_posted(conn, item["url"]): continue
                if not is_content_sufficient(item["title"], item["summary"]):
                    mark_posted(conn, item["url"], item["title"]); continue
                tweet_text = generate_tweet(anthropic, item["title"], item["summary"], item["source"])
                if not tweet_text:
                    mark_posted(conn, item["url"], item["title"]); continue
                tid = post_tweet(twitter, tweet_text)
                if tid:
                    mark_posted(conn, item["url"], item["title"], tid)
                    update_daily(conn)
                    add_pending_reply(conn, tid, item["url"])
                    posted = True; break

            c2, _ = get_daily_info(conn)
            log.info(f"Daily: {c2}/{DAILY_LIMIT}. {'Posted.' if posted else 'No suitable news.'}")
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run()
