
from ircstack.dispatch.events import JoinEvent
from ircstack.dispatch import event_handler

@event_handler(JoinEvent)
def spam(event):
    if event.network.nick == event.sender.nick:
        event.network.privmsg(event.channel, "Just testing some spam.")
        for x in range(10):
            event.network.privmsg_lazy(event.channel, "This is line %d." % x)
