from demibot import IRCBot
from ircstack.dispatch.events import ConsoleCommandEvent, event_handler

from ircstack.util import get_logger
log = get_logger(__name__)

@event_handler(ConsoleCommandEvent)
def on_command(event):
    #if log: log.debug('Got ConsoleCommandEvent with params: %s' % repr(event.params))
    if len(event.params) < 2: return

    if event.params[0] in IRCBot().networks:
        net = IRCBot().networks[event.params[0]]
        net.send_raw(net.Message(*event.params[1:]))
