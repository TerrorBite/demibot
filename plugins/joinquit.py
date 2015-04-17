
from random import choice

from ircstack.dispatch.events import JoinEvent, QuitEvent
from ircstack.dispatch import event_handler


#@event_handler(QuitEvent)
def handle_quit(event):
    if event.network.name.lower() == 'freenode':
        messages = [
                "Quit it and hit it. Or something.",
                "If you can't quit them, join them. Or something.",
                "Quitters are weak. Unless you're a smoker.",
                "These other guys are so negative. Gets me down.",
                ]
        event.network.privmsg('#ullarah', choice(messages))

@event_handler(JoinEvent)
def handle_join(event):
    if event.channel == '#ullarah':
        messages = [
                "Another player joins the fray.",
                "Welcome to Bot Central, enjoy your stay!",
                "These other guys are showing me up with their fancy messages.",
                "Testicles.",
                "Gee it's getting awfully noisy in here.",
                "Getting a bit crowded in here.",
                "I just farted.",
                "WELCOME TO DICKSAU wait wrong channel never mind",
                "Shitcock!",
                "Gee it's getting awfully noisy in here.",
                "Welp.",
                ]
        event.network.privmsg(event.channel, choice(messages))
