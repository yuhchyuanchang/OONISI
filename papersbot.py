#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# PapersBot
#
# purpose:  read journal RSS feeds and tweet selected entries
# license:  MIT License
# author:   François-Xavier Coudert
# e-mail:   fxcoudert@gmail.com
#
import os
import random
import re
import sys
import time
import urllib
from datetime import datetime, timedelta, timezone

import yaml

import atproto
import bs4
import feedparser
import filetype
import tweepy
from mastodon import Mastodon, MastodonError


# ==============================
# Settings
# ==============================
# RSS entry time filter (hours). Set to None to disable.
RECENT_HOURS = 24

# If RSS entry has no published/updated date, skip it (safe).
SKIP_IF_NO_DATE = True


# This is the regular expression that selects the papers of interest
regex = re.compile(r"""
  (   \b(MOF|MOFs|COF|COFs|ZIF|ZIFs)\b
    | metal.organic.framework
    | covalent.organic.framework
    | metal–organic.framework
    | covalent–organic.framework
    | imidazolate.framework
    | porous.coordination.polymer
    | framework.material
  )
  """, re.IGNORECASE | re.VERBOSE)


def entryMatches(entry):
    # Malformed entry
    if "title" not in entry:
        return False

    if regex.search(entry.title):
        return True
    if "summary" in entry:
        return bool(regex.search(entry.summary))
    else:
        return False


def entry_datetime_utc(entry):
    """
    Return datetime in UTC from feedparser entry, or None if unavailable.
    Prefer published date; fallback to updated date.
    """
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            # struct_time -> datetime(UTC)
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


# Find the URL for an image associated with the entry
def findImage(entry):
    if "description" not in entry:
        return None

    soup = bs4.BeautifulSoup(entry.description, "html.parser")
    img = soup.find("img")
    if img:
        img = img.get("src", "")
        if len(img) == 0:
            return None
        # If address is relative, append root URL
        if img.startswith("/"):
            p = urllib.parse.urlparse(entry.id)
            img = f"{p.scheme}://{p.netloc}" + img

    return img


# Convert string from HTML to plain text
def htmlToText(s):
    return bs4.BeautifulSoup(s, "html.parser").get_text()


def downloadImage(url):
    if not url:
        return None

    try:
        img, _ = urllib.request.urlretrieve(url)
    except Exception:
        return None

    kind = filetype.guess(img)
    if kind:
        # Rename to make type clear
        res = f"{img}.{kind.extension}"
        os.rename(img, res)
    else:
        # Not an image
        try:
            os.remove(img)
        except Exception:
            pass
        return None

    # Images smaller than 4 KB have a problem, and Twitter will complain
    if os.path.getsize(res) < 4096:
        os.remove(res)
        return None

    return res


# Helper functions for Bluesky, adapted from
# https://github.com/MarshalX/atproto/blob/main/examples/advanced_usage/auto_hyperlinks.py

def bluesky_extract_url_byte_positions(text, *, aggressive: bool, encoding='UTF-8'):
    """
    If aggressive is False, only links beginning http or https will be detected
    """
    encoded_text = text.encode(encoding)

    if aggressive:
        pattern = rb'(?:[\w+]+\:\/\/)?(?:[\w\d-]+\.)*[\w-]+[\.\:]\w+\/?(?:[\/\?\=\&\#\.\(\)]?[\w-]+)+\/?'
    else:
        pattern = rb'https?\:\/\/(?:[\w\d-]+\.)*[\w-]+[\.\:]\w+\/?(?:[\/\?\=\&\#\.\(\)]?[\w-]+)+\/?'

    matches = re.finditer(pattern, encoded_text)
    url_byte_positions = []
    for match in matches:
        url_bytes = match.group(0)
        url = url_bytes.decode(encoding)
        url_byte_positions.append((url, match.start(), match.end()))

    return url_byte_positions


def bluesky_post_with_links(client, text, image_file):
    """
    Send a skeet, identifying and handling links
    """
    # Determine locations of URLs in the post's text
    url_positions = bluesky_extract_url_byte_positions(text, aggressive=False)
    facets = []

    if image_file:
        with open(image_file, 'rb') as f:
            img_data = f.read()
        upload = client.com.atproto.repo.upload_blob(img_data)
        images = [atproto.models.AppBskyEmbedImages.Image(alt="TOC Graphic", image=upload.blob)]
        embed = atproto.models.AppBskyEmbedImages.Main(images=images)
    else:
        embed = None

    # AT requires URL to include http or https when creating the facet. Appends to URL if not present
    for link in url_positions:
        uri = link[0] if link[0].startswith('http') else f'https://{link[0]}'
        facets.append(
            atproto.models.AppBskyRichtextFacet.Main(
                features=[atproto.models.AppBskyRichtextFacet.Link(uri=uri)],
                index=atproto.models.AppBskyRichtextFacet.ByteSlice(byte_start=link[1], byte_end=link[2]),
            )
        )

    client.com.atproto.repo.create_record(
        atproto.models.ComAtprotoRepoCreateRecord.Data(
            repo=client.me.did,
            collection=atproto.models.ids.AppBskyFeedPost,
            record=atproto.models.AppBskyFeedPost.Record(
                created_at=client.get_current_time_iso(),
                text=text,
                facets=facets,
                embed=embed
            ),
        )
    )


# Connect to Twitter and authenticate
def initTwitter():
    if 'CONSUMER_KEY' in os.environ:
        cred = {'CONSUMER_KEY': os.environ['CONSUMER_KEY'],
                'CONSUMER_SECRET': os.environ['CONSUMER_SECRET'],
                'ACCESS_KEY': os.environ['ACCESS_KEY'],
                'ACCESS_SECRET': os.environ['ACCESS_SECRET']}
    else:
        with open("credentials.yml", "r") as f:
            cred = yaml.safe_load(f)

    # v1 API
    auth = tweepy.OAuthHandler(cred["CONSUMER_KEY"], cred["CONSUMER_SECRET"])
    auth.set_access_token(cred["ACCESS_KEY"], cred["ACCESS_SECRET"])
    v1 = tweepy.API(auth)

    # v2 API
    v2 = tweepy.Client(consumer_key=cred["CONSUMER_KEY"],
                       consumer_secret=cred["CONSUMER_SECRET"],
                       access_token=cred["ACCESS_KEY"],
                       access_token_secret=cred["ACCESS_SECRET"])

    print("Twitter authentification worked")
    return v1, v2


# Connect to Mastodon
def initMastodon():
    if 'MASTODON_API_BASE_URL' in os.environ:
        cred = {'API_BASE_URL': os.environ['MASTODON_API_BASE_URL'],
                'CLIENT_ID': os.environ['MASTODON_CLIENT_ID'],
                'CLIENT_SECRET': os.environ['MASTODON_CLIENT_SECRET'],
                'USER': os.environ['MASTODON_USER'],
                'PASSWORD': os.environ['MASTODON_PASSWORD']}
    else:
        with open("mastodon_credentials.yml", "r") as f:
            cred = yaml.safe_load(f)

    mastodon = Mastodon(client_id=cred["CLIENT_ID"], client_secret=cred["CLIENT_SECRET"], api_base_url=cred["API_BASE_URL"])
    token = mastodon.log_in(cred["USER"], cred["PASSWORD"])
    mastodon = Mastodon(access_token=token, api_base_url=cred["API_BASE_URL"])

    print("Mastodon authentification worked")
    return mastodon


# Connect to Bluesky
def initBluesky():
    if 'BLUESKY_HANDLE' in os.environ:
        cred = {'HANDLE': os.environ['BLUESKY_HANDLE'],
                'APP_PASSWORD': os.environ['BLUESKY_APP_PASSWORD']}
    else:
        with open("bluesky_credentials.yml", "r") as f:
            cred = yaml.safe_load(f)

    bluesky = atproto.Client()
    bluesky.login(cred['HANDLE'], cred['APP_PASSWORD'])

    print("Bluesky authentification worked")
    return bluesky


# Read our list of feeds from file
def readFeedsList():
    with open("feeds.txt", "r") as f:
        feeds = [s.partition("#")[0].strip() for s in f]
        return [s for s in feeds if s]


# Remove unwanted text some journals insert into the feeds
def cleanText(s):
    s = s.replace("[ASAP]", "")
    s = s.replace("\x0A", "")
    s = re.sub(r"\(arXiv:.+\)", "", s)
    return re.sub("\\s\\s+", " ", s).strip()


# Read list of feed items already posted
def readPosted():
    try:
        with open("posted.dat", "r") as f:
            return f.read().splitlines()
    except OSError:
        return []


class PapersBot:
    posted = []
    n_seen = 0
    n_tweeted = 0

    def __init__(self, doTweet=False):
        self.do_post = bool(doTweet)   # ←明示的に保存
        self.feeds = readFeedsList()
        self.posted = readPosted()

        # Read parameters from configuration file
        try:
            with open("config.yml", "r") as f:
                config = yaml.safe_load(f)
        except OSError:
            config = {}

        self.throttle = config.get("throttle", 0)
        self.wait_time = config.get("wait_time", 5)
        self.shuffle_feeds = config.get("shuffle_feeds", True)
        self.blacklist = config.get("blacklist", [])
        self.blacklist = [re.compile(s) for s in self.blacklist]

        # Shuffle feeds list
        if self.shuffle_feeds:
            random.shuffle(self.feeds)

        # Connect to Twitter / Bluesky / Mastodon only when actually posting
        if self.do_post:
            self.api_v1, self.api_v2 = initTwitter()
            try:
                self.bluesky = initBluesky()
            except Exception:
                print('Did not connect to Bluesky')
                self.bluesky = None
            try:
                self.mastodon = initMastodon()
            except Exception:
                print('Did not connect to Mastodon')
                self.mastodon = None
        else:
            self.api_v1 = None
            self.api_v2 = None
            self.bluesky = None
            self.mastodon = None

        urllen = 23
        imglen = 24
        self.maxlength = 280 - (urllen + 1) - imglen

        print(f"This is PapersBot running at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"Feed list has {len(self.feeds)} feeds\n")

    # Add to tweets posted (only called in real posting mode)
    def addToPosted(self, key):
        with open("posted.dat", "a+") as f:
            print(key, file=f)
        self.posted.append(key)

    # Send a tweet for a given feed entry
    def sendTweet(self, entry):
        title = cleanText(htmlToText(entry.title))
        length = self.maxlength

        # Usually the ID is the canonical URL, but not always
        if entry.id[:8] == "https://" or entry.id[:7] == "http://":
            url = entry.id
        else:
            url = entry.link

        # URL may be malformed
        if not (url[:8] == "https://" or url[:7] == "http://"):
            print(f"INVALID URL: {url}\n")
            return

        tweet_body = title[:length] + " " + url

        # URL may match our blacklist
        for regexp in self.blacklist:
            if regexp.search(url):
                print(f"BLACKLISTED: {tweet_body}\n")
                # In real mode: mark as posted to avoid repeating blacklisted content
                if self.do_post:
                    self.addToPosted(entry.id)
                return

        media = None
        mastodon_media = None
        image = findImage(entry)
        image_file = downloadImage(image)
        if image_file:
            print(f"IMAGE: {image}")
            if self.api_v1:
                media = [self.api_v1.media_upload(image_file).media_id]
            if self.mastodon:
                mastodon_media = [self.mastodon.media_post(image_file)]

        print(f"TWEET: {tweet_body}\n")

        # ---- Dry-run mode: do not post AND do not touch posted.dat ----
        if not self.do_post:
            if image_file:
                os.remove(image_file)
            return
        # --------------------------------------------------------------

        # Post to Twitter
        if self.api_v2:
            try:
                self.api_v2.create_tweet(text=tweet_body, media_ids=media)
            except tweepy.errors.TooManyRequests:
                print("ERROR: Too many requests, Twitter rate limit hit. Stopping now.\n")
                sys.exit(0)
            except tweepy.errors.TweepyException as e:
                if 187 in getattr(e, "api_codes", []):
                    print("ERROR: Tweet refused as duplicate\n")
                    # If Twitter says duplicate, still record as posted to avoid looping forever
                else:
                    print(f"ERROR: Tweet refused, {repr(e)}\n")
                    sys.exit(1)

        # Post to Bluesky
        if self.bluesky:
            try:
                bluesky_post_with_links(self.bluesky, tweet_body, image_file)
            except Exception as e:
                print(f"ERROR: Bluesky post refused: {e}\n")
                sys.exit(1)

        # Post to Mastodon
        if self.mastodon:
            try:
                self.mastodon.status_post(tweet_body, media_ids=mastodon_media)
            except MastodonError as e:
                print(f"ERROR: Toot refused: {e}\n")
                sys.exit(1)

        # Record as posted ONLY after successful posting attempts
        self.addToPosted(entry.id)
        self.n_tweeted += 1

        if image_file:
            os.remove(image_file)

        if self.api_v2 or self.mastodon:
            time.sleep(self.wait_time)

    # Main function, iterating over feeds and posting new items
    def run(self):
        now_utc = datetime.now(timezone.utc)
        cutoff = now_utc - timedelta(hours=RECENT_HOURS) if RECENT_HOURS is not None else None

        for feed in self.feeds:
            try:
                parsed_feed = feedparser.parse(feed)
            except ConnectionResetError as e:
                print("Failure to load feed at URL", feed)
                print("Exception info:", str(e))
                sys.exit(1)

            for entry in parsed_feed.entries:
                if not entryMatches(entry):
                    continue

                # Count relevant papers (matching regex)
                self.n_seen += 1

                # If no ID provided, use the link as ID
                if "id" not in entry:
                    entry.id = entry.link

                # --- 24h filter (based on RSS published/updated time) ---
                if cutoff is not None:
                    dt = entry_datetime_utc(entry)
                    if dt is None:
                        if SKIP_IF_NO_DATE:
                            continue
                    else:
                        if dt < cutoff:
                            continue
                # --------------------------------------------------------

                # Skip already posted
                if entry.id in self.posted:
                    continue

                # Post (or print in dry-run)
                self.sendTweet(entry)

                # Bail out if we have reached max number of tweets
                if self.throttle > 0 and self.n_tweeted >= self.throttle:
                    print(f"Max number of papers met ({self.throttle}), stopping now")
                    return

    def printStats(self):
        print(f"Number of relevant papers: {self.n_seen}")
        print(f"Number of papers tweeted: {self.n_tweeted}")

    def printTopTweets(self, count=20):
        tweets = self.api_v1.user_timeline(count=200)
        oldest = tweets[-1].created_at
        print(f"Top {count} recent tweets, by number of RT and likes, since {oldest}:\n")

        tweets = [(t.retweet_count + t.favorite_count, t.id, t) for t in tweets]
        tweets.sort(reverse=True)
        for _, _, t in tweets[0:count]:
            url = f"https://twitter.com/{t.user.screen_name}/status/{t.id}"
            print(f"{t.retweet_count} RT {t.favorite_count} likes: {url}")
            print(f"    {t.created_at}")
            print(f"    {t.text}\n")


def main():
    options_allowed = ["--do-not-tweet", "--top-tweets"]
    for arg in sys.argv[1:]:
        if arg not in options_allowed:
            print(f"Unknown option: {arg}")
            sys.exit(1)

    # True = real posting; False = dry-run
    doTweet = "--do-not-tweet" not in sys.argv

    bot = PapersBot(doTweet)

    if "--top-tweets" in sys.argv:
        bot.printTopTweets()
        sys.exit(0)

    bot.run()
    bot.printStats()


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# PapersBot
#
# purpose:  read journal RSS feeds and tweet selected entries
# license:  MIT License
# author:   François-Xavier Coudert
# e-mail:   fxcoudert@gmail.com
#
import os
import random
import re
import sys
import time
import urllib
from datetime import datetime, timedelta, timezone

import yaml

import atproto
import bs4
import feedparser
import filetype
import tweepy
from mastodon import Mastodon, MastodonError


# ==============================
# Settings
# ==============================
# RSS entry time filter (hours). Set to None to disable.
RECENT_HOURS = 24

# If RSS entry has no published/updated date, skip it (safe).
SKIP_IF_NO_DATE = True


# This is the regular expression that selects the papers of interest
regex = re.compile(r"""
  (   \b(MOF|MOFs|COF|COFs|ZIF|ZIFs)\b
    | metal.organic.framework
    | covalent.organic.framework
    | metal–organic.framework
    | covalent–organic.framework
    | imidazolate.framework
    | porous.coordination.polymer
    | framework.material
  )
  """, re.IGNORECASE | re.VERBOSE)


def entryMatches(entry):
    # Malformed entry
    if "title" not in entry:
        return False

    if regex.search(entry.title):
        return True
    if "summary" in entry:
        return bool(regex.search(entry.summary))
    else:
        return False


def entry_datetime_utc(entry):
    """
    Return datetime in UTC from feedparser entry, or None if unavailable.
    Prefer published date; fallback to updated date.
    """
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            # struct_time -> datetime(UTC)
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


# Find the URL for an image associated with the entry
def findImage(entry):
    if "description" not in entry:
        return None

    soup = bs4.BeautifulSoup(entry.description, "html.parser")
    img = soup.find("img")
    if img:
        img = img.get("src", "")
        if len(img) == 0:
            return None
        # If address is relative, append root URL
        if img.startswith("/"):
            p = urllib.parse.urlparse(entry.id)
            img = f"{p.scheme}://{p.netloc}" + img

    return img


# Convert string from HTML to plain text
def htmlToText(s):
    return bs4.BeautifulSoup(s, "html.parser").get_text()


def downloadImage(url):
    if not url:
        return None

    try:
        img, _ = urllib.request.urlretrieve(url)
    except Exception:
        return None

    kind = filetype.guess(img)
    if kind:
        # Rename to make type clear
        res = f"{img}.{kind.extension}"
        os.rename(img, res)
    else:
        # Not an image
        try:
            os.remove(img)
        except Exception:
            pass
        return None

    # Images smaller than 4 KB have a problem, and Twitter will complain
    if os.path.getsize(res) < 4096:
        os.remove(res)
        return None

    return res


# Helper functions for Bluesky, adapted from
# https://github.com/MarshalX/atproto/blob/main/examples/advanced_usage/auto_hyperlinks.py

def bluesky_extract_url_byte_positions(text, *, aggressive: bool, encoding='UTF-8'):
    """
    If aggressive is False, only links beginning http or https will be detected
    """
    encoded_text = text.encode(encoding)

    if aggressive:
        pattern = rb'(?:[\w+]+\:\/\/)?(?:[\w\d-]+\.)*[\w-]+[\.\:]\w+\/?(?:[\/\?\=\&\#\.\(\)]?[\w-]+)+\/?'
    else:
        pattern = rb'https?\:\/\/(?:[\w\d-]+\.)*[\w-]+[\.\:]\w+\/?(?:[\/\?\=\&\#\.\(\)]?[\w-]+)+\/?'

    matches = re.finditer(pattern, encoded_text)
    url_byte_positions = []
    for match in matches:
        url_bytes = match.group(0)
        url = url_bytes.decode(encoding)
        url_byte_positions.append((url, match.start(), match.end()))

    return url_byte_positions


def bluesky_post_with_links(client, text, image_file):
    """
    Send a skeet, identifying and handling links
    """
    # Determine locations of URLs in the post's text
    url_positions = bluesky_extract_url_byte_positions(text, aggressive=False)
    facets = []

    if image_file:
        with open(image_file, 'rb') as f:
            img_data = f.read()
        upload = client.com.atproto.repo.upload_blob(img_data)
        images = [atproto.models.AppBskyEmbedImages.Image(alt="TOC Graphic", image=upload.blob)]
        embed = atproto.models.AppBskyEmbedImages.Main(images=images)
    else:
        embed = None

    # AT requires URL to include http or https when creating the facet. Appends to URL if not present
    for link in url_positions:
        uri = link[0] if link[0].startswith('http') else f'https://{link[0]}'
        facets.append(
            atproto.models.AppBskyRichtextFacet.Main(
                features=[atproto.models.AppBskyRichtextFacet.Link(uri=uri)],
                index=atproto.models.AppBskyRichtextFacet.ByteSlice(byte_start=link[1], byte_end=link[2]),
            )
        )

    client.com.atproto.repo.create_record(
        atproto.models.ComAtprotoRepoCreateRecord.Data(
            repo=client.me.did,
            collection=atproto.models.ids.AppBskyFeedPost,
            record=atproto.models.AppBskyFeedPost.Record(
                created_at=client.get_current_time_iso(),
                text=text,
                facets=facets,
                embed=embed
            ),
        )
    )


# Connect to Twitter and authenticate
def initTwitter():
    if 'CONSUMER_KEY' in os.environ:
        cred = {'CONSUMER_KEY': os.environ['CONSUMER_KEY'],
                'CONSUMER_SECRET': os.environ['CONSUMER_SECRET'],
                'ACCESS_KEY': os.environ['ACCESS_KEY'],
                'ACCESS_SECRET': os.environ['ACCESS_SECRET']}
    else:
        with open("credentials.yml", "r") as f:
            cred = yaml.safe_load(f)

    # v1 API
    auth = tweepy.OAuthHandler(cred["CONSUMER_KEY"], cred["CONSUMER_SECRET"])
    auth.set_access_token(cred["ACCESS_KEY"], cred["ACCESS_SECRET"])
    v1 = tweepy.API(auth)

    # v2 API
    v2 = tweepy.Client(consumer_key=cred["CONSUMER_KEY"],
                       consumer_secret=cred["CONSUMER_SECRET"],
                       access_token=cred["ACCESS_KEY"],
                       access_token_secret=cred["ACCESS_SECRET"])

    print("Twitter authentification worked")
    return v1, v2


# Connect to Mastodon
def initMastodon():
    if 'MASTODON_API_BASE_URL' in os.environ:
        cred = {'API_BASE_URL': os.environ['MASTODON_API_BASE_URL'],
                'CLIENT_ID': os.environ['MASTODON_CLIENT_ID'],
                'CLIENT_SECRET': os.environ['MASTODON_CLIENT_SECRET'],
                'USER': os.environ['MASTODON_USER'],
                'PASSWORD': os.environ['MASTODON_PASSWORD']}
    else:
        with open("mastodon_credentials.yml", "r") as f:
            cred = yaml.safe_load(f)

    mastodon = Mastodon(client_id=cred["CLIENT_ID"], client_secret=cred["CLIENT_SECRET"], api_base_url=cred["API_BASE_URL"])
    token = mastodon.log_in(cred["USER"], cred["PASSWORD"])
    mastodon = Mastodon(access_token=token, api_base_url=cred["API_BASE_URL"])

    print("Mastodon authentification worked")
    return mastodon


# Connect to Bluesky
def initBluesky():
    if 'BLUESKY_HANDLE' in os.environ:
        cred = {'HANDLE': os.environ['BLUESKY_HANDLE'],
                'APP_PASSWORD': os.environ['BLUESKY_APP_PASSWORD']}
    else:
        with open("bluesky_credentials.yml", "r") as f:
            cred = yaml.safe_load(f)

    bluesky = atproto.Client()
    bluesky.login(cred['HANDLE'], cred['APP_PASSWORD'])

    print("Bluesky authentification worked")
    return bluesky


# Read our list of feeds from file
def readFeedsList():
    with open("feeds.txt", "r") as f:
        feeds = [s.partition("#")[0].strip() for s in f]
        return [s for s in feeds if s]


# Remove unwanted text some journals insert into the feeds
def cleanText(s):
    s = s.replace("[ASAP]", "")
    s = s.replace("\x0A", "")
    s = re.sub(r"\(arXiv:.+\)", "", s)
    return re.sub("\\s\\s+", " ", s).strip()


# Read list of feed items already posted
def readPosted():
    try:
        with open("posted.dat", "r") as f:
            return f.read().splitlines()
    except OSError:
        return []


class PapersBot:
    posted = []
    n_seen = 0
    n_tweeted = 0

    def __init__(self, doTweet=False):
        self.do_post = bool(doTweet)   # ←明示的に保存
        self.feeds = readFeedsList()
        self.posted = readPosted()

        # Read parameters from configuration file
        try:
            with open("config.yml", "r") as f:
                config = yaml.safe_load(f)
        except OSError:
            config = {}

        self.throttle = config.get("throttle", 0)
        self.wait_time = config.get("wait_time", 5)
        self.shuffle_feeds = config.get("shuffle_feeds", True)
        self.blacklist = config.get("blacklist", [])
        self.blacklist = [re.compile(s) for s in self.blacklist]

        # Shuffle feeds list
        if self.shuffle_feeds:
            random.shuffle(self.feeds)

        # Connect to Twitter / Bluesky / Mastodon only when actually posting
        if self.do_post:
            self.api_v1, self.api_v2 = initTwitter()
            try:
                self.bluesky = initBluesky()
            except Exception:
                print('Did not connect to Bluesky')
                self.bluesky = None
            try:
                self.mastodon = initMastodon()
            except Exception:
                print('Did not connect to Mastodon')
                self.mastodon = None
        else:
            self.api_v1 = None
            self.api_v2 = None
            self.bluesky = None
            self.mastodon = None

        urllen = 23
        imglen = 24
        self.maxlength = 280 - (urllen + 1) - imglen

        print(f"This is PapersBot running at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"Feed list has {len(self.feeds)} feeds\n")

    # Add to tweets posted (only called in real posting mode)
    def addToPosted(self, key):
        with open("posted.dat", "a+") as f:
            print(key, file=f)
        self.posted.append(key)

    # Send a tweet for a given feed entry
    def sendTweet(self, entry):
        title = cleanText(htmlToText(entry.title))
        length = self.maxlength

        # Usually the ID is the canonical URL, but not always
        if entry.id[:8] == "https://" or entry.id[:7] == "http://":
            url = entry.id
        else:
            url = entry.link

        # URL may be malformed
        if not (url[:8] == "https://" or url[:7] == "http://"):
            print(f"INVALID URL: {url}\n")
            return

        tweet_body = title[:length] + " " + url

        # URL may match our blacklist
        for regexp in self.blacklist:
            if regexp.search(url):
                print(f"BLACKLISTED: {tweet_body}\n")
                # In real mode: mark as posted to avoid repeating blacklisted content
                if self.do_post:
                    self.addToPosted(entry.id)
                return

        media = None
        mastodon_media = None
        image = findImage(entry)
        image_file = downloadImage(image)
        if image_file:
            print(f"IMAGE: {image}")
            if self.api_v1:
                media = [self.api_v1.media_upload(image_file).media_id]
            if self.mastodon:
                mastodon_media = [self.mastodon.media_post(image_file)]

        print(f"TWEET: {tweet_body}\n")

        # ---- Dry-run mode: do not post AND do not touch posted.dat ----
        if not self.do_post:
            if image_file:
                os.remove(image_file)
            return
        # --------------------------------------------------------------

        # Post to Twitter
        if self.api_v2:
            try:
                self.api_v2.create_tweet(text=tweet_body, media_ids=media)
            except tweepy.errors.TooManyRequests:
                print("ERROR: Too many requests, Twitter rate limit hit. Stopping now.\n")
                sys.exit(0)
            except tweepy.errors.TweepyException as e:
                if 187 in getattr(e, "api_codes", []):
                    print("ERROR: Tweet refused as duplicate\n")
                    # If Twitter says duplicate, still record as posted to avoid looping forever
                else:
                    print(f"ERROR: Tweet refused, {repr(e)}\n")
                    sys.exit(1)

        # Post to Bluesky
        if self.bluesky:
            try:
                bluesky_post_with_links(self.bluesky, tweet_body, image_file)
            except Exception as e:
                print(f"ERROR: Bluesky post refused: {e}\n")
                sys.exit(1)

        # Post to Mastodon
        if self.mastodon:
            try:
                self.mastodon.status_post(tweet_body, media_ids=mastodon_media)
            except MastodonError as e:
                print(f"ERROR: Toot refused: {e}\n")
                sys.exit(1)

        # Record as posted ONLY after successful posting attempts
        self.addToPosted(entry.id)
        self.n_tweeted += 1

        if image_file:
            os.remove(image_file)

        if self.api_v2 or self.mastodon:
            time.sleep(self.wait_time)

    # Main function, iterating over feeds and posting new items
    def run(self):
        now_utc = datetime.now(timezone.utc)
        cutoff = now_utc - timedelta(hours=RECENT_HOURS) if RECENT_HOURS is not None else None

        for feed in self.feeds:
            try:
                parsed_feed = feedparser.parse(feed)
            except ConnectionResetError as e:
                print("Failure to load feed at URL", feed)
                print("Exception info:", str(e))
                sys.exit(1)

            for entry in parsed_feed.entries:
                if not entryMatches(entry):
                    continue

                # Count relevant papers (matching regex)
                self.n_seen += 1

                # If no ID provided, use the link as ID
                if "id" not in entry:
                    entry.id = entry.link

                # --- 24h filter (based on RSS published/updated time) ---
                if cutoff is not None:
                    dt = entry_datetime_utc(entry)
                    if dt is None:
                        if SKIP_IF_NO_DATE:
                            continue
                    else:
                        if dt < cutoff:
                            continue
                # --------------------------------------------------------

                # Skip already posted
                if entry.id in self.posted:
                    continue

                # Post (or print in dry-run)
                self.sendTweet(entry)

                # Bail out if we have reached max number of tweets
                if self.throttle > 0 and self.n_tweeted >= self.throttle:
                    print(f"Max number of papers met ({self.throttle}), stopping now")
                    return

    def printStats(self):
        print(f"Number of relevant papers: {self.n_seen}")
        print(f"Number of papers tweeted: {self.n_tweeted}")

    def printTopTweets(self, count=20):
        tweets = self.api_v1.user_timeline(count=200)
        oldest = tweets[-1].created_at
        print(f"Top {count} recent tweets, by number of RT and likes, since {oldest}:\n")

        tweets = [(t.retweet_count + t.favorite_count, t.id, t) for t in tweets]
        tweets.sort(reverse=True)
        for _, _, t in tweets[0:count]:
            url = f"https://twitter.com/{t.user.screen_name}/status/{t.id}"
            print(f"{t.retweet_count} RT {t.favorite_count} likes: {url}")
            print(f"    {t.created_at}")
            print(f"    {t.text}\n")


def main():
    options_allowed = ["--do-not-tweet", "--top-tweets"]
    for arg in sys.argv[1:]:
        if arg not in options_allowed:
            print(f"Unknown option: {arg}")
            sys.exit(1)

    # True = real posting; False = dry-run
    doTweet = "--do-not-tweet" not in sys.argv

    bot = PapersBot(doTweet)

    if "--top-tweets" in sys.argv:
        bot.printTopTweets()
        sys.exit(0)

    bot.run()
    bot.printStats()


if __name__ == "__main__":
    main()

