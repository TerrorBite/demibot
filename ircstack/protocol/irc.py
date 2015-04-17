"""
IRCStack/Protocol/IRC
High-level IRC protocol handling.
"""

# Python imports
# (none)

# Set up logging
from ircstack.util import get_logger, hrepr
log = get_logger()

# ircstack imports
import ircstack.network
from ircstack.dispatch import Dispatcher
from ircstack.dispatch.events import EventListener, event_handler
from ircstack.dispatch.async import DelayedTask
import ircstack.dispatch.events as events
from ircstack.protocol import isupport
from collections import namedtuple

#TODO: Remove this when PluginLoader moved fully to demibot
from demibot import PluginLoader

#dispatch = Dispatcher.dispatch

handlers = {}

# Constants
LAZY=0
NORMAL=1
URGENT=2
IMMEDIATE=3

class handles(object):
    "Decorator to register a method as a handler for a particular type of IRC command/reply."
    def __init__(self, *commands):
        self.commands = []
        for cmd in commands:
            if isinstance(cmd, str):
                cmd = cmd.decode('utf-8')
            if isinstance(cmd, int):
                cmd = u"%03d" % cmd
            self.commands.append(cmd.upper())

    def __call__(self, func):
        global handlers
        for c in self.commands:
            handlers[c] = func
        return func

# "005 default": Our default is the value given by the ISUPPORT draft as the
#   default for this ISUPPORT parameter.
# "005/RFC default": As above, but the default also matches the RFC 1459 spec.
# "005 required": The ISUPPORT draft says that a supporting server is required
#   to send that parameter; thus no default is specified. We choose a sane
#   default, based on RFC 1459 if possible.

class IRCNetwork(EventListener):
    """An IRCNetwork stores the state of a connection to an IRC network, and acts as an object-oriented
    interface for performing standard IRC commands and actions.
    
    It handles incoming IRCMessages and parses them to handle various commands and replies appropriately.
    It tracks important data such as the channels joined, current nickname, modes, etc. It parses the
    initial ISUPPORT numeric from the server to learn about the specific quirks of the network, in order
    to act accordingly.

    Most importantly, the IRCNetwork creates and fires appropriate events that can be subscribed to by
    plugins. The IRCNetwork does not take any actions itself beyond connecting and joining certain channels
    as defined in the network's config. After that point, all actions are carried out by plugins, with the
    IRCNetwork serving mainly as a framework to facilitate them.
    """
    log = get_logger()

    def __init__(self, config, autoconnect=False):
        """Creates a new IRCNetwork instance based on the given config."""

        # Store config
        self.conf = config

        # Compile server list from config
        serverlist = [ircstack.network.IRCServer(uri) for uri in config.servers]

        # DEBUG: Using a sane encoder
        self._encoder = ircstack.network.IRCEncoder('iso-8859-1')
        self._conn = ircstack.network.IRCConnection(self, serverlist, encoder=self._encoder)

        # Important settings - use RFC1459-specified defaults in case server doesn't send ISUPPORT
        self.isupport = isupport.ISupport()

        # Informational stuff
        self.name = self.conf.name
        self.nick = None

        self.enabled_plugins = {}

        # DelayedTask to abort STARTTLS
        self._starttls_task = None

        # Subscribe to events (via EventListener superclass)
        super(IRCNetwork, self).__init__()

        # Load/enable plugins
        for k in config.plugins.keys():
            try:
                #TODO: Move all config/plugin stuff out to demibot
                PluginLoader.get_plugin(k).enable(self)
                self.log.debug("Loaded plugin %s in network %s" % (k, repr(self)))
            except:
                self.log.exception("Failed to load plugin %s in network %s" % (k, repr(self)))

        self._nickindex = 0

    def __repr__(self):
        return hrepr(self)

    def Message(self, command, *params):
        """
        Factory function for an IRCMessage suitable for sending to the network.
        """
        # Silly plugin writers are probably going to feed us byte strings
        # If we get one, assume it's UTF-8 and convert it
        if isinstance(command, str): command = command.decode('utf-8','replace')
        params = [param.decode('utf-8','replace') if isinstance(param, str) else param for param in params]

        return ircstack.network.IRCMessage(self._encoder, None, command, params)
    
    def _ctcp(self, cmd, type_, target, params):
        return self.Message(cmd, target, u"\u0001%s%s\u0001" % (type_.upper(), u' ' + ' '.join(params) if params else u''))

    def CTCPRequest(self, type_, target, *params):
        """Factory function to generate an IRCMessage consisting of a single CTCP request."""
        return self._ctcp(u'PRIVMSG', type_, target, params)

    def CTCPReply(self, type_, target, *params):
        """Factory function to generate an IRCMessage consisting of a single CTCP reply."""
        return self._ctcp(u'NOTICE', type_, target, params)


    def connect(self):
        # Connect to our config-defined IRC servers
        self._conn.connect()

    def send_raw(self, ircmessage, priority=NORMAL):
        "Send a raw IRCMessage to the server."
        self._conn.send(ircmessage, priority)

    ### IRC Commands

    def privmsg(self, target, message):
        self.send_raw(self.Message(u'PRIVMSG', target, message))
    def lazy_privmsg(self, target, message):
        #TODO: Better name
        self.send_raw(self.Message(u'PRIVMSG', target, message), LAZY)

    def quit(self, message):
        # Quit from IRC
        self.send_raw(self.Message(u'QUIT', message), URGENT)

    def join(self, channel, key=None):
        """Join an individual IRC channel."""
        self.send_raw(self.Message(u'JOIN', channel))

    def kick(self, channel, nick):
        self.send_raw(self.Message(u'KICK', nick), URGENT)

    def mode(self, target, mode):
        self.send_raw(self.Message(u'MODE', target, mode), NORMAL)

    def next_nick(self):
        "Uses the next nickname defined in config."
        # Get next nickname
        if self._nickindex < len(self.conf.nick):
            self._nickindex += 1
            self.nick = self.conf.nick[self._nickindex]
        else:
            self.nick = u"%s_" % self.nick
        self.send_raw(self.Message(u'NICK', self.nick))


    def join_all(self, channels):
        """
        Joins many channels in a single command.
        Expects a dict with channel names as keys.
        Values are the channel key (password), or none.
        """
        c = channels.items()
        self.send_raw(self.Message(u'JOIN', ','.join(i[0] for i in c), ','.join('' if i[1] is None else i[1] for i in c) ))

    ### Event Callbacks

    def on_connected(self):
        """
        Called when we are successfully connected to the IRC server,
        before any commands are sent or received.
        """
        # Send IRC login information

        self._nickindex = 0
        nickname, ident, realname = (self.conf.nick[0], self.conf.ident, self.conf.realname)

        # These are ignored for a client connection.
        hostname, servername = (u'*', u'*')

        # XXX: EXPERIMENTAL
        #self.send_raw(self.Message(u'MODE', u'IRCv3'))
        self._conn.starttls_begin()
        # after 5 seconds, assume STARTTLS was ignored
        self._starttls_task = DelayedTask(self._conn.starttls_abort, 5.0)()

        # TODO: Send PASS here
        self.send_raw(self.Message(u'NICK', nickname), IMMEDIATE)
        self.send_raw(self.Message(u'USER', ident, hostname, servername, realname), IMMEDIATE)

        self.nick = nickname

    def on_disconnected(self):
        """
        Called when the IRC network disconnects us.
        """
        # TODO: Replace with an Event
        pass

    @event_handler(events.MessageReceivedEvent)
    def on_MessageReceived(self, evt):
        """Called when an IRCEvent is raised.

        This method looks for an appropriate internal handler method (as registered using
        the @handler decorator) for the particular type of message received from the IRC
        server, and calls it if it finds one. That handler will be responsible for modifying
        the IRCNetwork's state as required, and generating the appropriate IRCEvent subclass
        for use by plugins."""

        # Shouldn't need this if statement, but just to be safe
        if evt.network is self:
            msg = evt.message
            cmd = msg.command.upper()
            # Call the registered handler for this message, if there is one
            if msg.command in handlers:
                handlers[msg.command.upper()](self, msg)
                # Also fire an IRCEvent for anyone interested in the raw message
                # (NOTE: IRCEvent is fired AFTER internal message handling is complete)
                events.IRCEvent(self, msg).dispatch()
            else:
                if len(cmd) == 3 and cmd.isdigit():
                    self.log.warning("Received unknown %s numeric: %s" % (cmd,msg))
                else:
                    self.log.warning("Received unknown %s command: %s" % (cmd,msg))
                self.on_unhandled(evt)
        else:
            # This shouldn't happen
            self.log.warning("%s was called for an event thrown by %s" % (repr(self), repr(evt.network)))
            self.log.debug(evt.message)

##### IRCNetwork        ###############
##### Internal Handlers ###############
    
    # Handlers below are divided into three sections:

    # RFC 1459 Commands:
    # Contains handlers for all the standard IRC commands as defined in RFC 1459.

    # RFC 1459 Numerics:
    # Contains handlers for all of the numeric reply codes as defined in RFC 1459.

    # RFC 2812:
    # Contains handlers for IRC commands and numeric replies defined in RFC 2812.

    # Miscellaneous:
    # Contains handlers for non-standard IRC commands and reply codes that are not
    # defined in either RFC 1459 or RFC 2812, i.e. for proprietary IRCd features.

    # Resource: https://www.alien.net.au/irc/irc2numerics.html

    def on_unhandled(self, evt):
        """
        Called when there is no handler available for a particular message type.
        """
        events.UnknownMessageEvent(self, evt.message).dispatch()
        pass

    ############
    # RFC 1459 #
    # Commands #
    ############

    @handles('NICK')
    def handle_nick(self, msg):
        "Handles nickname change messages."
        event = events.NickEvent(self, msg)
        if event.sender.nick is self.nick:
            # our nickname has changed
            self.log.info("Our nickname on %s changed to %s" % (self.name, msg.params[1]))
            self.nick = msg.params[1]
        event.dispatch()

    @handles('PRIVMSG')
    def handle_privmsg(self, msg):
        events.PrivmsgEvent(self, msg)
        if '#' in msg.params[0]:
            self.log.info(u'(%d) [%s] <%s> %s' % (msg.seq, msg.params[0], events.Sender(msg).nick, msg.message))
            events.ChannelMessageEvent(self, msg).dispatch()
        else:
            self.log.info(u'(%d) <%s> %s' % (msg.seq, events.Sender(msg).nick, msg.message))
            events.PrivateMessageEvent(self, msg).dispatch()

        if msg.message[0] == self.conf.prefix_char:
            events.CommandEvent(self, msg).dispatch()


    @handles('NOTICE')
    def handle_notice(self, msg):
        self.log.info(u"NOTICE from %s: %s" % (msg.prefix, msg.message))
        events.NoticeEvent(self, msg).dispatch()

    @handles('QUIT')
    def handle_join(self, msg):
        self.log.info(u"%s has quit: %s" % (msg.prefix, msg.message))
        events.QuitEvent(self, msg).dispatch()

    @handles('JOIN')
    def handle_join(self, msg):
        self.log.info(u"%s has joined %s" % (msg.prefix, msg.message))
        events.JoinEvent(self, msg).dispatch()

    @handles('PART')
    def handle_part(self, msg):
        if len(msg.params) > 1:
            self.log.info(u"%s has left %s (%s)" % (msg.prefix, msg.params[0], msg.message))
        else:
            self.log.info(u"%s has left %s" % (msg.prefix, msg.params[0]))
        # TODO: Remove user from relevant IRCChannels
        events.PartEvent(self, msg).dispatch()

    @handles('MODE')
    def handle_mode(self, msg):
        target, params = msg.params[0], msg.params[1:]

        # TODO: NYI
        return

        # Is target a user (usually us), or a channel?
        if target[0] in self.isupport.chantypes:
            events.ChannelModeEvent(self, msg).dispatch()
        else:
            events.UserModeEvent(self, msg).dispatch()
        events.ModeEvent(self, msg).dispatch()

    ############
    # RFC 1459 #
    # Numerics #
    ############

    @handles(1)
    def handle_welcome(self, msg):
        self.log.info(msg.message)
        log.debug(events.WelcomeEvent.subscribers.items())
        events.WelcomeEvent(self, msg).dispatch()

    @handles(2)
    def handle_yourhost(self, msg):
        "Your host is X, running version Y"
        self.log.info(msg.message)

    @handles(3)
    def handle_created(self, msg):
        "This server was created..."
        self.log.info(msg.message)

    @handles(4)
    def handle_myinfo(self, msg):
        server_name, version, user_modes, chan_modes = msg.params[:4]
        # Do something with these

    @handles(5)
    def handle_isupport(self, msg):
        # TODO: Proper parsing of RPL_ISUPPORT

        # NOTE that while RFC2812 defines 005 as RPL_BOUNCE, the only IRCd known
        # to implement RPL_BOUNCE has changed it to 010 instead.

        # This handler implements the RPL_ISUPPORT draft laid out at:
        # http://tools.ietf.org/html/draft-brocklesby-irc-isupport-03
        # It is, however, backwards compatible with previous versions of the draft.

        # Reference: http://www.irc.org/tech_docs/005.html

        # Strip initial nick and trailing text
        params = msg.params[1:-1]
        
        for param in params:
            self.isupport.from_param(*param.split('=', 1))

    @handles(372) # MOTD message
    def handle_motd(self, msg):
        self.log.debug(msg.message)

    @handles(375) # Start of MOTD message
    def handle_startofmotd(self, msg):
        pass

    @handles(376) # End of MOTD message
    def handle_endofmotd(self, msg):

        # Automatically join configured channels
        def autojoin():
            chans = {}
            for chan in self.conf.channels:
                if self.conf.channels[chan].autojoin is not False:
                    chans[chan] = self.conf.channels[chan].key
            if chans: self.join_all(chans)

        # Execute delayed autojoin task
        DelayedTask(autojoin, 2.0)()
        events.EndOfMOTDEvent(self, msg).dispatch()

    @handles(437)
    def handle_unavailresource(self, msg):
        """
        RPL_UNAVAILRESOURCE: Defined in RFC2812.
        - Returned by a server to a user trying to join a channel
          currently blocked by the channel delay mechanism.
        - Returned by a server to a user trying to change nickname
          when the desired nickname is blocked by the nick delay
          mechanism.
        """

        if msg.params[1] == self.nick:
            # The nickname we selected is temporarily unavailable -
            # it is probably being held by services
            self.log.info('%s: Nickname "%s" appears to be locked down by Services' % (self.name, self.nick))
            self.next_nick()
        else:
            self.log.debug(msg)

    @handles(451)
    def handle_notregistered(self, msg):
        """
        Handles the ERR_NOTREGISTERED numeric, received when the server
        rejects a command because we haven't registered with USER yet.
        """
        if self._starttls_task is not None:
            # Abort STARTTLS if server rejects us
            self._conn.starttls_abort()
            self._starttls_task.cancel()
            self._starttls_task = None
        self.log.info(msg)
    @handles(670)
    def handle_starttls(self, msg):
        """
        This reply is sent in response to a STARTTLS command
        to indicate to us that it is safe to switch the socket over to SSL mode.
        """
        self.log.info("Activating Transport Layer Security")
        self._conn.starttls_complete()

    @handles(691)
    def handle_starttls_failed(self, msg):
        self.log.warn("Server reported an error activating TLS")
        self._conn.starttls_abort()


class IRCChannel(object):
    """
    Represents an IRC channel.
    """
    def __init__(self):
        # TODO: IRCChannel implementation
        pass

class IRCUser(object):
    """
    Represents an IRC user.
    """
    def __init__(self):
        # TODO: IRCUser implementation
        pass
log.debug("IRC Protocol Framework initialized.")
