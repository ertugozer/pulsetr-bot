import os, time, hashlib, sqlite3, logging, feedparser, tweepy, re, json
from anthropic import Anthropic
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

TWITTER_API_KEY      = os.environ["TWITTER_API_KEY"]
TWITTER_API_SECRET   = os.environ["TWITTER_API_SECRET"]
TWITTER_ACCESS_TOKEN = os.environ["TWITTER_ACCESS_TOKEN"]
TWITTER_ACCESS_SECRET= os.environ["TWITTER_ACCESS_SECRET"]
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]

PEAK_WINDOWS         = [(7,9),(11,13),(16,17),(20,23)]
MIN_INTERVAL         = 20    # her tweet arasi min dakika (breaking dahil)
BREAKING_EXTRA_WAIT  = 15    # breaking sonrasi min bekleme (dakika)
DAILY_LIMIT          = 14
TOPIC_DAILY_LIMIT    = 2     # ayni konudan gunde max tweet
CHECK_INTERVAL       = 600
SELF_REPLY_DELAY     = 360
BREAKING_SCORE       = 8
MIN_SCORE            = 4

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
    conn.execute("CREATE TABLE IF NOT EXISTS posted (hash TEXT PRIMARY KEY, title TEXT, posted_at TEXT, tweet_id TEXT, topic TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS daily_count (date TEXT PRIMARY KEY, count INTEGER DEFAULT 0, last_tweet_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS pending_replies (tweet_id TEXT PRIMARY KEY, url TEXT, reply_at TEXT)")
    # topic_count tablosu: gun + topic bazli sayac
    conn.execute("CREATE TABLE IF NOT EXISTS topic_count (date TEXT, topic TEXT, count INTEGER DEFAULT 0, PRIMARY KEY(date, topic))")
    conn.commit()
    return conn

def is_posted(conn, url):
    h = hashlib.md5(url.encode()).hexdigest()
    return conn.execute("SELECT 1 FROM posted WHERE hash=?", (h,)).fetchone() is not None

def mark_posted(conn, url, title, tweet_id="", topic="genel"):
    h = hashlib.md5(url.encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO posted VALUES(?,?,?,?,?)",
                 (h, title, datetime.now(timezone.utc).isoformat(), tweet_id, topic))
    conn.commit()

def get_daily_info(conn):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute("SELECT count, last_tweet_at FROM daily_count WHERE date=?", (today,)).fetchone()
    return (row[0], row[1]) if row else (0, None)

def update_daily(conn, topic="genel"):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO daily_count(date,count,last_tweet_at) VALUES(?,1,?) ON CONFLICT(date) DO UPDATE SET count=count+1, last_tweet_at=?",
                 (today, now, now))
    conn.execute("INSERT INTO topic_count(date,topic,count) VALUES(?,?,1) ON CONFLICT(date,topic) DO UPDATE SET count=count+1",
                 (today, topic))
    conn.commit()

def get_topic_count(conn, topic):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute("SELECT count FROM topic_count WHERE date=? AND topic=?", (today, topic)).fetchone()
    return row[0] if row else 0

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
    return any(s <= h <= e for s,e in PEAK_WINDOWS)

def mins_since(last_at):
    if not last_at: return 999
    return (datetime.now(timezone.utc) - datetime.fromisoformat(last_at)).total_seconds() / 60

def fetch_news():
    seen_titles = set()
    items = []
    for f in RSS_FEEDS:
        try:
            for e in feedparser.parse(f["url"]).entries[:5]:
                title = e.get("title","").strip()
                # Basit duplicate baslik kontrolu (farkli kaynaktan ayni haber)
                title_key = re.sub(r'[^a-z0-9]', '', title.lower())[:40]
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)
                items.append({"title": title, "url": e.get("link","").strip(),
                              "summary": e.get("summary","").strip()[:400], "source": f["source"]})
        except Exception as ex:
            log.warning(f"RSS [{f['source']}]: {ex}")
    return items

def score_and_filter(client, items):
    if not items:
        return []
    news_list = "\n".join([f"{i+1}. {it['title']} | {it['summary'][:80]}" for i,it in enumerate(items)])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role":"user","content":f"""Turkce haberleri degerlendir.

Her haber icin JSON:
- score: 1-10
- ok: true/false
- topic: tek kelime konu etiketi (deprem, gazze, ekonomi, siyaset, spor, teknoloji, saglik, dunya, turkiye, genel)

Skor:
9-10: Olum/felaket/tarihi karar/savas/ekonomik kriz
7-8: Onemli siyasi/ekonomik gelisme
5-6: Guncel siyaset/ekonomi/spor
3-4: Rutin/belirsiz
1-2: Reklam/muglak

ok=false: somut bilgi yok, reklam, anlamsiz

SADECE JSON array:
[{{"index":1,"score":8,"ok":true,"topic":"deprem"}}]

Haberler:
{news_list}"""}]
    )
    try:
        raw = re.sub(r'```json|```', '', resp.content[0].text.strip()).strip()
        scores = json.loads(raw)
        result = []
        for s in scores:
            idx = s["index"] - 1
            if 0 <= idx < len(items) and s.get("ok", False) and s.get("score", 0) >= MIN_SCORE:
                result.append((s["score"], s.get("topic","genel"), items[idx]))
        result.sort(key=lambda x: x[0], reverse=True)
        return result
    except Exception as e:
        log.error(f"Score error: {e} | {resp.content[0].text[:200]}")
        return []

def fix_hashtags(tweet):
    return re.sub(r'#([A-Za-z\u00C0-\u017E]+)\s+([A-Za-z\u00C0-\u017E]+)',
                  lambda m: '#' + m.group(1) + m.group(2), tweet)

def validate_tweet(tweet):
    if not tweet or len(tweet) < 30: return None
    if "YETERSIZ_HABER" in tweet: return None
    if re.search(r'https?://|www\.', tweet): return None
    tweet = fix_hashtags(tweet)
    tags = re.findall(r'#(\S+)', tweet)
    for extra in tags[2:]:
        tweet = tweet.replace(f"#{extra}", "").strip()
    return tweet[:270].strip()

def generate_tweet(client, title, summary, source, score, is_breaking):
    h = (datetime.now(timezone.utc).hour + 3) % 24
    if is_breaking:
        fmt = "SON DAKIKA: Max 120 karakter, 1 emoji, net somut bilgi."
    elif 7 <= h <= 9:
        fmt = "Sabah ozeti: 3 bullet madde, somut rakam. Son: 'Kaydet takip et'"
    elif 11 <= h <= 13:
        fmt = "Somut bilgi + acik uclu soru. Max 200 karakter."
    else:
        fmt = "Carpici bilgi + yapici yorum + soru. Max 230 karakter."

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        messages=[{"role":"user","content":f"""Turkce haber tweeti yaz. Skor: {score}/10.

Haber: {title}
Ozet: {summary}

Format: {fmt}

KURALLAR:
1. URL/link EKLEME
2. Bos ifade yasak: 'takip edin', 'resmi kaynaktan bakin'
3. Yetersiz haberse: YETERSIZ_HABER
4. Hashtag bosluksuz, max 2: #SonDakika YANLIS -> #Gundem DOGRU
5. Alintilar: kisa tirmak icinde ver + baglam ekle (max 60 karakter alinti)
6. Tarafsiz ton, taraf tutma
7. Pozitif/yapici dil

Sadece tweeti yaz:"""}]
    )
    return validate_tweet(resp.content[0].text.strip())

def post_tweet(twitter, text):
    try:
        r = twitter.create_tweet(text=text)
        log.info(f"Tweet: {text[:80]}")
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
    log.info("PulseTR Bot v5 started.")
    conn = init_db()
    anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
    twitter = tweepy.Client(consumer_key=TWITTER_API_KEY, consumer_secret=TWITTER_API_SECRET,
                            access_token=TWITTER_ACCESS_TOKEN, access_token_secret=TWITTER_ACCESS_SECRET)
    while True:
        try:
            # Self-reply'leri gonder
            for tid, url in get_due_replies(conn):
                post_reply(twitter, tid, f"Kaynak: {url}")
                delete_reply(conn, tid)
                time.sleep(5)

            count, last_at = get_daily_info(conn)
            if count >= DAILY_LIMIT:
                log.info(f"Daily limit ({DAILY_LIMIT}). Sleeping...")
                time.sleep(CHECK_INTERVAL)
                continue

            # Her durumda minimum interval kontrolu
            m = mins_since(last_at)
            if m < MIN_INTERVAL:
                wait = int((MIN_INTERVAL - m) * 60)
                log.info(f"Min interval {m:.1f}/{MIN_INTERVAL}min. Wait {wait}s...")
                time.sleep(min(wait, CHECK_INTERVAL))
                continue

            news = fetch_news()
            unposted = [it for it in news if it["url"] and it["title"] and not is_posted(conn, it["url"])]
            if not unposted:
                log.info("No new items.")
                time.sleep(CHECK_INTERVAL)
                continue

            scored = score_and_filter(anthropic, unposted)
            if not scored:
                log.info("All filtered out.")
                time.sleep(CHECK_INTERVAL)
                continue

            # En iyi haberi sec, topic limitini kontrol et
            selected = None
            for top_score, topic, item in scored:
                tc = get_topic_count(conn, topic)
                if tc >= TOPIC_DAILY_LIMIT:
                    log.info(f"Topic limit reached: {topic} ({tc}/{TOPIC_DAILY_LIMIT}). Skipping.")
                    mark_posted(conn, item["url"], item["title"], topic=topic)
                    continue
                selected = (top_score, topic, item)
                break

            if not selected:
                log.info("All topics at daily limit. Sleeping...")
                time.sleep(CHECK_INTERVAL)
                continue

            top_score, topic, top_item = selected
            is_breaking = top_score >= BREAKING_SCORE

            # Normal haberler icin peak kontrolu
            if not is_breaking and not is_peak():
                h = (datetime.now(timezone.utc).hour + 3) % 24
                log.info(f"Off-peak ({h}:xx TR). Normal news waits...")
                time.sleep(CHECK_INTERVAL)
                continue

            log.info(f"Selected: score={top_score} topic={topic} breaking={is_breaking} | {top_item['title'][:60]}")

            tweet_text = generate_tweet(anthropic, top_item["title"], top_item["summary"],
                                        top_item["source"], top_score, is_breaking)
            if not tweet_text:
                mark_posted(conn, top_item["url"], top_item["title"], topic=topic)
                time.sleep(60)
                continue

            tid = post_tweet(twitter, tweet_text)
            if tid:
                mark_posted(conn, top_item["url"], top_item["title"], tid, topic)
                update_daily(conn, topic)
                add_pending_reply(conn, tid, top_item["url"])
                log.info(f"{'BREAKING' if is_breaking else 'Normal'} | topic={topic} | Daily: {count+1}/{DAILY_LIMIT}")

                # Breaking sonrasi ekstra bekleme
                if is_breaking:
                    log.info(f"Breaking posted. Extra wait {BREAKING_EXTRA_WAIT}min...")
                    time.sleep(BREAKING_EXTRA_WAIT * 60)
                    continue

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run()
