import os
import time
import hashlib
import sqlite3
import logging
import feedparser
import tweepy
from anthropic import Anthropic
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

TWITTER_API_KEY        = os.environ["TWITTER_API_KEY"]
TWITTER_API_SECRET     = os.environ["TWITTER_API_SECRET"]
TWITTER_ACCESS_TOKEN   = os.environ["TWITTER_ACCESS_TOKEN"]
TWITTER_ACCESS_SECRET  = os.environ["TWITTER_ACCESS_SECRET"]
ANTHROPIC_API_KEY      = os.environ["ANTHROPIC_API_KEY"]

MAX_TWEETS_PER_DAY = 48
CHECK_INTERVAL     = 1800

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
    conn.execute("""CREATE TABLE IF NOT EXISTS posted (hash TEXT PRIMARY KEY, title TEXT, posted_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_count (date TEXT PRIMARY KEY, count INTEGER DEFAULT 0)""")
    conn.commit()
    return conn

def is_posted(conn, url):
    h = hashlib.md5(url.encode()).hexdigest()
    return conn.execute("SELECT 1 FROM posted WHERE hash=?", (h,)).fetchone() is not None

def mark_posted(conn, url, title):
    h = hashlib.md5(url.encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO posted VALUES (?,?,?)", (h, title, datetime.utcnow().isoformat()))
    conn.commit()

def daily_tweet_count(conn):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    row = conn.execute("SELECT count FROM daily_count WHERE date=?", (today,)).fetchone()
    return row[0] if row else 0

def increment_daily_count(conn):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn.execute("INSERT INTO daily_count(date, count) VALUES(?,1) ON CONFLICT(date) DO UPDATE SET count=count+1", (today,))
    conn.commit()

def fetch_latest_news(limit_per_feed=5):
    news = []
    for feed in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed["url"])
            for entry in parsed.entries[:limit_per_feed]:
                news.append({"title": entry.get("title","").strip(), "url": entry.get("link","").strip(), "summary": entry.get("summary","").strip()[:500], "source": feed["source"]})
        except Exception as e:
            log.warning(f"RSS error [{feed['source']}]: {e}")
    return news

def generate_tweet(client, title, summary, source, url):
    prompt = f"""Bir Turkce haber tweeti yaz.

Haber basligi: {title}
Kaynak: {source}
Ozet: {summary}

Kurallar:
- Maksimum 240 karakter (URL icin 23 karakter yer birak)
- Turkce yaz, sade ve anlasilir
- Haber dilinde ol, tarafsiz
- 2-3 alakali hashtag ekle sonuna
- Asla haber metnini kopyalama
- Sadece tweeti yaz"""

    response = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=300, messages=[{"role": "user", "content": prompt}])
    tweet_text = response.content[0].text.strip()
    if len(tweet_text) > 240:
        tweet_text = tweet_text[:237] + "..."
    return f"{tweet_text}\n\n{url}"

def post_tweet(twitter, text):
    try:
        twitter.create_tweet(text=text)
        log.info(f"Tweet posted: {text[:80]}...")
        return True
    except tweepy.TweepyException as e:
        log.error(f"Tweet error: {e}")
        return False

def run():
    log.info("PulseTR Bot started.")
    conn = init_db()
    anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
    twitter = tweepy.Client(consumer_key=TWITTER_API_KEY, consumer_secret=TWITTER_API_SECRET, access_token=TWITTER_ACCESS_TOKEN, access_token_secret=TWITTER_ACCESS_SECRET)

    while True:
        try:
            if daily_tweet_count(conn) >= MAX_TWEETS_PER_DAY:
                log.info("Daily limit reached. Waiting...")
                time.sleep(CHECK_INTERVAL)
                continue

            news_items = fetch_latest_news()
            log.info(f"Fetched {len(news_items)} news items.")
            tweeted = 0

            for item in news_items:
                if not item["url"] or not item["title"]:
                    continue
                if is_posted(conn, item["url"]):
                    continue
                tweet = generate_tweet(anthropic, item["title"], item["summary"], item["source"], item["url"])
                if post_tweet(twitter, tweet):
                    mark_posted(conn, item["url"], item["title"])
                    increment_daily_count(conn)
                    tweeted += 1
                    time.sleep(90)
                if daily_tweet_count(conn) >= MAX_TWEETS_PER_DAY:
                    break

            log.info(f"Tweeted {tweeted} this round. Next check in {CHECK_INTERVAL//60} min.")
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log.info("Bot stopped.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run()
