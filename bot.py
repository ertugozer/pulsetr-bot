import os
import time
import hashlib
import sqlite3
import logging
import feedparser
import tweepy
from anthropic import Anthropic
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

TWITTER_API_KEY        = os.environ["TWITTER_API_KEY"]
TWITTER_API_SECRET     = os.environ["TWITTER_API_SECRET"]
TWITTER_ACCESS_TOKEN   = os.environ["TWITTER_ACCESS_TOKEN"]
TWITTER_ACCESS_SECRET  = os.environ["TWITTER_ACCESS_SECRET"]
ANTHROPIC_API_KEY      = os.environ["ANTHROPIC_API_KEY"]

# Peak saatler (Türkiye GMT+3) - (saat_baslangic, saat_bitis, max_tweet)
PEAK_WINDOWS = [
    (7, 9, 4),    # Sabah: 07:30-09:30, 4 tweet
    (11, 13, 4),  # Öğle: 11:30-13:30, 4 tweet
    (16, 17, 3),  # Akşam: 16:00-17:30, 3 tweet
    (20, 22, 4),  # Gece: 20:00-22:30, 4 tweet
]
MIN_INTERVAL_MINUTES = 20   # Tweetler arası minimum süre
DAILY_LIMIT = 17            # Günlük max tweet
CHECK_INTERVAL = 600        # 10 dakikada bir kontrol

RSS_FEEDS = [
    {"url": "https://www.ntv.com.tr/son-dakika.rss",         "source": "NTV"},
    {"url": "https://www.hurriyet.com.tr/rss/anasayfa",      "source": "Hurriyet"},
    {"url": "https://www.sabah.com.tr/rss",                  "source": "Sabah"},
    {"url": "https://www.cnnturk.com/feed/rss/all/news",     "source": "CNN Turk"},
    {"url": "https://www.bbc.com/turkce/index.xml",          "source": "BBC Turkce"},
    {"url": "https://tr.euronews.com/rss",                   "source": "Euronews TR"},
]

def init_db():
    conn = sqlite3.connect("pulsetr.db")
    conn.execute("CREATE TABLE IF NOT EXISTS posted (hash TEXT PRIMARY KEY, title TEXT, posted_at TEXT, tweet_id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS daily_count (date TEXT PRIMARY KEY, count INTEGER DEFAULT 0, last_tweet_at TEXT)")
    conn.commit()
    return conn

def is_posted(conn, url):
    h = hashlib.md5(url.encode()).hexdigest()
    return conn.execute("SELECT 1 FROM posted WHERE hash=?", (h,)).fetchone() is not None

def mark_posted(conn, url, title, tweet_id=""):
    h = hashlib.md5(url.encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO posted VALUES (?,?,?,?)", (h, title, datetime.now(timezone.utc).isoformat(), tweet_id))
    conn.commit()

def get_daily_info(conn):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute("SELECT count, last_tweet_at FROM daily_count WHERE date=?", (today,)).fetchone()
    return (row[0], row[1]) if row else (0, None)

def update_daily_count(conn, tweet_id=""):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO daily_count(date, count, last_tweet_at) VALUES(?,1,?) ON CONFLICT(date) DO UPDATE SET count=count+1, last_tweet_at=?", (today, now, now))
    conn.commit()

def is_peak_hour():
    hour_tr = (datetime.now(timezone.utc).hour + 3) % 24
    for start, end, _ in PEAK_WINDOWS:
        if start <= hour_tr <= end:
            return True
    return False

def peak_limit_for_current_window():
    hour_tr = (datetime.now(timezone.utc).hour + 3) % 24
    for start, end, limit in PEAK_WINDOWS:
        if start <= hour_tr <= end:
            return limit
    return 0

def minutes_since_last_tweet(last_tweet_at):
    if not last_tweet_at:
        return 999
    last = datetime.fromisoformat(last_tweet_at)
    diff = datetime.now(timezone.utc) - last
    return diff.total_seconds() / 60

def fetch_latest_news(limit_per_feed=5):
    news = []
    for feed in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed["url"])
            for entry in parsed.entries[:limit_per_feed]:
                news.append({
                    "title": entry.get("title","").strip(),
                    "url": entry.get("link","").strip(),
                    "summary": entry.get("summary","").strip()[:500],
                    "source": feed["source"]
                })
        except Exception as e:
            log.warning(f"RSS error [{feed['source']}]: {e}")
    return news

def generate_tweet(client, title, summary, source):
    hour_tr = (datetime.now(timezone.utc).hour + 3) % 24
    if 20 <= hour_tr or hour_tr < 2:
        fmt = "breaking"
    elif hour_tr < 10:
        fmt = "list"
    else:
        fmt = "question"

    if fmt == "question":
        instruction = "Haber ozeti yaz + sonunda kisa bir soru sor (orn: Sizce bu nasil etkiler? / Bu karar dogru muydu?). Soru reply getiriyor."
    elif fmt == "list":
        instruction = "Haberi madde madde yaz (• ile 3-4 madde). Rakam ve veri kullan. Repost alir."
    else:
        instruction = "Son dakika formatinda yaz. Baslangica 'Son Dakika:' veya emoji koy. Kisa ve carpici."

    prompt = f"""Turkce haber tweeti yaz. Link EKLEME - link olmadan yaz.

Haber: {title}
Kaynak: {source}
Ozet: {summary}

Format: {instruction}

Kurallar:
- Max 240 karakter
- 1-2 alakali hashtag sonuna
- Link yok (cok onemli - link ekleme)
- Sadece tweeti yaz, aciklama ekleme"""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    tweet = resp.content[0].text.strip()
    if len(tweet) > 240:
        tweet = tweet[:237] + "..."
    return tweet

def post_tweet_with_reply(twitter, tweet_text, url):
    try:
        # Ana tweet - link yok
        resp = twitter.create_tweet(text=tweet_text)
        tweet_id = resp.data["id"]
        log.info(f"Tweet posted: {tweet_text[:60]}...")

        # Kaynak linki reply olarak
        try:
            reply_text = f"Kaynak: {url}"
            twitter.create_tweet(text=reply_text, in_reply_to_tweet_id=tweet_id)
            log.info("Source reply posted.")
        except Exception as e:
            log.warning(f"Reply error: {e}")

        return tweet_id
    except tweepy.TweepyException as e:
        log.error(f"Tweet error: {e}")
        return None

def run():
    log.info("PulseTR Bot v2 started.")
    conn = init_db()
    anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
    twitter = tweepy.Client(
        consumer_key=TWITTER_API_KEY,
        consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN,
        access_token_secret=TWITTER_ACCESS_SECRET,
    )

    while True:
        try:
            daily_count, last_tweet_at = get_daily_info(conn)
            mins_since = minutes_since_last_tweet(last_tweet_at)

            if daily_count >= DAILY_LIMIT:
                log.info(f"Daily limit ({DAILY_LIMIT}) reached. Waiting...")
                time.sleep(CHECK_INTERVAL)
                continue

            if not is_peak_hour():
                log.info(f"Off-peak hour (TR: {(datetime.now(timezone.utc).hour+3)%24}:xx). Waiting...")
                time.sleep(CHECK_INTERVAL)
                continue

            if mins_since < MIN_INTERVAL_MINUTES:
                wait = int((MIN_INTERVAL_MINUTES - mins_since) * 60)
                log.info(f"Interval not met ({mins_since:.1f} min). Waiting {wait}s...")
                time.sleep(min(wait, CHECK_INTERVAL))
                continue

            news = fetch_latest_news()
            log.info(f"Fetched {len(news)} items.")
            tweeted = 0

            for item in news:
                if not item["url"] or not item["title"]:
                    continue
                if is_posted(conn, item["url"]):
                    continue

                tweet_text = generate_tweet(anthropic, item["title"], item["summary"], item["source"])
                tweet_id = post_tweet_with_reply(twitter, tweet_text, item["url"])

                if tweet_id:
                    mark_posted(conn, item["url"], item["title"], tweet_id)
                    update_daily_count(conn)
                    tweeted += 1
                    break  # Peak window'da 1 tweet at, sonra MIN_INTERVAL bekle

            log.info(f"Tweeted {tweeted}. Daily: {daily_count+tweeted}/{DAILY_LIMIT}. Next check in {CHECK_INTERVAL//60} min.")
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log.info("Bot stopped.")
            break
        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run()
