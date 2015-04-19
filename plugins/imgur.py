import json
import urllib
import urllib2
import re

re_imgur = re.compile(r'imgur\.com/(?:gallery/)?([A-Za-z0-9]+)')

from ircstack.dispatch.events import ChannelMessageEvent, event_handler
from ircstack.util import get_logger
log = get_logger(__name__)

def on_enable(network):
    global conf
    conf = network.conf.plugins['imgur']

@event_handler(ChannelMessageEvent)
def imgur(event):
    match = re_imgur.search(event.message)
    if match:
        image = match.group(1)
        log.info(image)
        req_gallery = urllib2.Request("https://api.imgur.com/3/gallery/image/%s.json" % image, None,
                {"Authorization": "Client-ID 71f3cf62e4dfa93"})
        req_image = urllib2.Request("https://api.imgur.com/3/image/%s.json" % image, None,
                {"Authorization": "Client-ID 71f3cf62e4dfa93"})
        result = None
        try:
            result = json.loads(urllib2.urlopen(req_gallery).read())['data']
        except urllib2.HTTPError as e:
            if e.code == 404:
                try:
                    result = json.loads(urllib2.urlopen(req_image).read())['data']
                except urllib2.HTTPError as e:
                    if e.code == 404:
                        return event.reply('[Imgur] Error: That image does not seem to exist.')
                    return log.exception("Failed to get image info for %s" % image)
            elif e.code == 503:
                return event.reply("[Imgur is over capacity]")
        
        log.info(repr(result))
        name = result['title'] if 'title' in result else None
        nsfw = ('NSFW: ' if result['nsfw'] else '') if 'nsfw' in result else ''
        views = str(result['views']) if 'views' in result else 'unknown' 
        if name:
            event.reply('[Imgur] %s%s (%s views)' % (nsfw, name, views))
        else:
            event.reply('[Imgur] %s%s views' % (nsfw, views))
        subreddit = result['section'] if 'section' in result else None
        imgurl = result['link'] if 'link' in result else None
        try:
            if subreddit:
                url = urllib2.urlopen("http://www.reddit.com/r/%s/api/info.json?%s" % (subreddit, urllib.urlencode({'url':imgurl})))
            else:
                url = urllib2.urlopen("http://www.reddit.com/api/info.json?%s" % (urllib.urlencode({'url':imgurl}),))
        except urllib2.HTTPError as e:
            if e.code == 429:
                # Too fast
                return event.reply('[Reddit API rate limit exceeded]')
            elif e.code == 503:
                return event.reply('[Reddit is down for maintenance]')
            else:
                return log.exception('Reddit API call failed')
        results = json.loads(url.read())['data']['children']
        if results:
            r = results[0]['data']
            event.reply('[Reddit] %s"%s" Submitted to /r/%s by %s, %d points: http://redd.it/%s' % ('NSFW: ' if r['over_18'] else '', r['title'],  subreddit, r['author'], r['score'], r['id']))
        
