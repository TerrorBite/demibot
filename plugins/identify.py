
from ircstack.dispatch.events import CommandEvent, EndOfMOTDEvent, event_handler

@event_handler(EndOfMOTDEvent)
def on_ready(event):
    conf = event.network.conf.plugins['identify']
    username = conf['username'] if 'username' in conf else None
    password = conf['password'] if 'password' in conf else None
    if password:
        if username:
            event.network.send_raw(event.network.Message(u'ns', u'identify', username, password))
        else:
            event.network.send_raw(event.network.Message(u'ns', u'identify', password))
