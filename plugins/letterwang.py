import hashlib

from ircstack.dispatch.events import ChannelMessageEvent, event_handler

intdiff=1

common100 = ['the', 'be', 'to', 'of', 'and', 'a', 'in', 'that', 'have', 'i', 'it', 'for', 'not', 'on', 'with', 'he', 'as', 'you', 'do', 'at', 'this', 'but', 'his', 'by', 'from', 'they', 'we', 'say', 'her', 'she', 'or', 'an', 'will', 'my', 'one', 'all', 'would', 'there', 'their', 'what', 'so', 'up', 'out', 'if', 'about', 'who', 'get', 'which', 'go', 'me', 'when', 'make', 'can', 'like', 'time', 'no', 'just', 'him', 'know', 'take', 'person', 'into', 'year', 'your', 'good', 'some', 'could', 'them', 'see', 'other', 'than', 'then', 'now', 'look', 'only', 'come', 'its', 'over', 'think', 'also', 'back', 'after', 'use', 'two', 'how', 'our', 'work', 'first', 'well', 'way', 'even', 'new', 'want', 'because', 'any', 'these', 'give', 'day', 'most', 'us']

def diff2int(diff):
    return (2**256-1)/int(2**diff)

def str2int_le(binstr):
    l = len(binstr)
    val = ord(binstr[-1]) << ((l-1)<<3)
    return val+str2int_le(binstr[:-1]) if l>1 else val

def str2int_be(binstr):
    l = len(binstr)
    val = ord(binstr[0]) << ((l-1)<<3)
    return val+str2int_be(binstr[1:]) if l>1 else val

def on_enable(network):
    global intdiff
    conf = network.conf.plugins['letterwang']
    difficulty = conf['diff'] if 'diff' in conf else 2.0
    intdiff = diff2int(difficulty)

@event_handler(ChannelMessageEvent)
def letterwang(event):
    match=False
    for word in event.message.encode('utf-8').split():
        if word.lower() not in common100:
            h = hashlib.sha256(word.lower()).digest()
            if str2int_be(h) < intdiff: match=True
    if match:
        event.reply("%s: That's letterwang!" % (event.sender.nick,))

