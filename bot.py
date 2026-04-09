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

# Bu hashtagler algoritmada cok kalabalik ve penalize - KESINLIKLE YASAK
BANNED_HASHTAGS = [
    "SonDakika","Sondakika","sondakika",
    "Son","Dakika",  # yanlis bolunme engellemek icin
    "Breaking","BreakingNews",
    "Haber","Haberler","GundemTurkiye",
    "Turkey","Turkiye","News","TR",
]

# Izin verilen niş hashtagler (bunlari kullanmasi istenir)
ALLOWED_HASHTAG_EXAMPLES = "#Ekonomi #Borsa #BIST #Enflasyon #Dolar #Faiz #Deprem #Teknoloji #Yapay Zeka #Enerji #Saglik #Spor #Futbol #Siyaset #Dis Politika #NATO #AB #ABD"

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
    vague_phrases = [
        "detaylar icin","detaylar için","resmi kaynak","takip edin",
        "guncellenecek","güncellenecek","aciklama bekleniyor","açıklama bekleniyor",
        "bilgi gelecek","haberleri takip","daha fazla bilgi",
        "guncellenecektir","güncellenecektir","guncel bilgi","güncel bilgi",
        "bilgiler guncel","bilgileri guncel","aciklanacak","açıklanacak","bekleyiniz"
    ]
    for phrase in vague_phrases:
        if phrase in combined:
            return False
    has_number = bool(re.search(r'\d', combined))
    has_location = any(loc in combined for loc in ["istanbul","ankara","izmir","türkiye","turkey","avrupa","abd","rusya","çin","almanya","fransa","irak","suriye","iran","israel","israil"])
    has_person = any(p in combined for p in ["erdoğan","erdogan","bakan","başbakan","cumhurbaşkan","trump","putin","zelensky","macron","netanyahu"])
    if not (has_number or has_location or has_person):
        return False
    if len(title) < 20:
        return False
    return True

def clean_tweet(tweet):
    """Tweet kalite kontrolu ve temizligi"""
    if not tweet or len(tweet) < 20:
        return None

    # Link kontrolu
    if re.search(r'https?://|www\.', tweet):
        log.warning("Link detected in tweet, rejecting.")
        return None

    # Yasak hashtag kontrolu - hem tek kelime hem birlesik
    hashtags_in_tweet = re.findall(r'#(\w+)', tweet)
    for tag in hashtags_in_tweet:
        if tag in BANNED_HASHTAGS:
            log.warning(f"Banned hashtag #{tag} found, removing...")
            tweet = re.sub(r'#' + tag + r'\b', '', tweet).strip()

    # 3+ hashtag kontrolu - fazlalari sil
    hashtags_in_tweet = re.findall(r'#\w+', tweet)
    if len(hashtags_in_tweet) > 2:
        log.warning(f"Too many hashtags ({len(hashtags_in_tweet)}), keeping first 2.")
        keep = hashtags_in_tweet[:2]
        for tag in hashtags_in_tweet[2:]:
            tweet = tweet.replace(tag, '').strip()

    # Bos alinti/tirnak kontrolu
    if tweet.count('"') >= 2 and len(tweet) < 80:
        log.warning("Short quote-only tweet, rejecting.")
        return None

    # Yetersizlik sinyalleri
    bad_phrases = ["yetersiz_haber","YETERSIZ_HABER","detaylar için","resmi kaynaklara","takip edin","#Son Dakika","#Son dakika"]
    for p in bad_phrases:
        if p.lower() in tweet.lower():
            log.warning(f"Bad phrase detected: {p}")
            return None

    tweet = ' '.join(tweet.split())  # fazla bosluk temizle

    if len(tweet) > 270:
        tweet = tweet[:267] + "..."

    return tweet

def generate_tweet(client, title, summary, source):
    h = (datetime.now(timezone.utc).hour + 3) % 24

    if 7 <= h <= 9:
        fmt_inst = "Liste formatı: her madde • ile başlasın, max 3 madde, somut veri/rakam kullan. Son satırda: 'Kaydet ↗ takip et' yaz."
    elif 11 <= h <= 13:
        fmt_inst = "Kısa somut bilgi ver (rakam, yer, kişi), sonra gerçekten merak uyandıran açık uçlu soru sor. Max 200 karakter."
    elif 16 <= h <= 17:
        fmt_inst = "Son dakika formatı. Max 110 karakter. Sadece haber içeriği, somut bilgi. Başlangıca 1 emoji koy."
    else:
        fmt_inst = "Çarpıcı somut bilgiyle başla. Yapıcı bağlam ekle. Soru ile bitir. Max 230 karakter."

    prompt = f"""Sen PulseTR için Türkçe haber tweeti yazıyorsun.

Haber: {title}
Kaynak: {source}
Özet: {summary}

Format talimatı: {fmt_inst}

KESIN KURALLAR:
1. URL/link/http/www YASAK - tweet içinde kesinlikle link olmayacak
2. Muğlak ifadeler YASAK: "detaylar için", "resmi kaynaklara bakın", "takip edin", "güncellenecek"
3. Haber yetersizse sadece YETERSIZ_HABER yaz
4. Max 1-2 hashtag, SADECE niş ve ilgili olanlar: {ALLOWED_HASHTAG_EXAMPLES}
5. Bu hashtagler KESİNLİKLE YASAK (çok genel, penalize): #SonDakika #Son #Dakika #Haber #Breaking #TR #Turkey
6. Hashtagleri붙붙 yaz, boşluksuz: #SonDakika DEĞİL, #Ekonomi EVET
7. Siyasi söylemleri birebir aktarma - olayı tarafsız özetle
8. Pozitif/yapıcı ton - Grok negatif içeriği kısıtlıyor
9. "Like if / RT if" YASAK

İYİ TWEET ÖRNEKLERİ:
- "Merkez Bankası faizi %42.5'te sabit tuttu. Son 6 aydır değişmedi. Sizce doğru karar mı? #Ekonomi"
- "• BIST100: +%2.3 (9.840 puan)\n• Dolar: 38.4 TL\n• Altın: 3.240 TL\nKaydet ↗ #Borsa"
- "🔴 Bursa'da 4.2 büyüklüğünde deprem. AFAD: can kaybı yok. #Deprem"

KÖTÜ TWEET ÖRNEKLERİ (bunları yazma):
- "...dedi. #Son Dakika" (hashtag bölünmüş, genel, penalize)
- "Detaylar için takip edin" (boş içerik)
- Erdoğan'ın sözleri tırnak içinde uzun alıntı (siyasi söylem)

Sadece tweeti yaz. Yetersizse YETERSIZ_HABER yaz."""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=350,
        messages=[{"role":"user","content":prompt}]
    )
    raw = resp.content[0].text.strip()

    if "YETERSIZ_HABER" in raw:
        return None

    return clean_tweet(raw)

def post_tweet(twitter, text):
    try:
        r = twitter.create_tweet(text=text)
        log.info(f"Tweet OK: {text[:70]}...")
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
    log.info("PulseTR Bot v3.2 started.")
    conn = init_db()
    anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
    twitter = tweepy.Client(
        consumer_key=TWITTER_API_KEY, consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN, access_token_secret=TWITTER_ACCESS_SECRET
    )

    while True:
        try:
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
                log.info(f"Off-peak ({h}:xx TR).")
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

                if not is_content_sufficient(item["title"], item["summary"]):
                    log.info(f"SKIP (vague): {item['title'][:50]}")
                    mark_posted(conn, item["url"], item["title"])
                    continue

                tweet_text = generate_tweet(anthropic, item["title"], item["summary"], item["source"])

                if tweet_text is None:
                    log.info(f"SKIP (rejected): {item['title'][:50]}")
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
