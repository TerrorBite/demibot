
# NOTE: ircstack.network should avoid importing from any ircstack packages
# except itself and dispatch.

__all__ = ['IRCConnection', 'IRCSocketManager', 'NoValidAddressError']

# Python imports
import errno
import random
import socket
import re
import time
#from collections import deque

# Set up logging
from ircstack.util import get_logger
log = get_logger(__name__)

# ircstack imports
from ircstack.util import hrepr, namedtuple, Hooks
from ircstack.dispatch.async import AsyncDelayed, SyncRepeating, Sync
import ircstack.dispatch.events as events
from ircstack.network import IRCSocketManager
from ircstack.network.sockets import SendThrottledSocket

# Line matching regex
reline = re.compile(r'(?::([^ ]+) )?([^ ]+)(?: (.*))?$')

#: Default hooks.
#blank_hooks = (lambda: None, lambda _: None)

# ircstack.network.irc
class IRCConnection(object):
    """
    Represents and handles a connection to an IRC server.

    THE FOLLOWING DESCRIPTION IS OUT OF DATE

    It is responsible for creating a socket and connecting it to an IRC server on
    the network. It is responsible for retrying the connection until it succeeds.
    It can accept a list of different server addresses to try, and will cycle
    through them until it finds a working one.

    If disconnected, the IRCConnection will attempt to reconnect automatically,
    unless it has received an "ERROR :Closing Link" message from the server, in
    which case it will cowardly refuse to reconnect. This ERROR response is
    generally sent by the server to notify the client that the connection is about
    to be deliberately closed.

    It is not responsible for actually reading the socket - instead it hands the
    socket to the IRCSocketManager, which handles reading raw data from the socket,
    and buffering input and output. If the socket disconnects or errors, the
    IRCSocketManager will notify this IRCConnection and then discard both the
    socket and its reference to this instance.

    It is responsible for certain very basic IRC operations which are listed in
    their completeness below:

    * sending the initial commands necessary to connect to the IRC server
    * transparently responding to PING messages as they arrive
    * Translating incoming messages to/from the correct character encoding

    It is responsible for low-level parsing of the IRC protocol at a basic level.
    It is completely agnostic about higher-level IRC protocol; it should happily
    deal with anything that looks like valid IRC syntax and throw it at the
    Dispatcher to deal with, allowing it to handle network-specific extensions to
    the IRC protocol such as WATCH, or the IRC profile of SASL.

    Most importantly, IRCConnection instances do not hold any high-level state
    regarding the IRC session. The only state it is concerned about is whether the
    connection is alive or not.

    * Parsing generates a tuple of (type, sender, params) which (excluding PINGs)
      are submitted to the Dispatcher.
    * PING messages are immediately processed without delay within the current thread.
    """

    log = get_logger()
    def __init__(self, address, ssl=False, encoder=None):
        """
        Creates an IRCConnection.
        
        network: The IRCNetwork that owns this connection.
        address: The server to connect to, as a (host, port) tuple.
        ssl: Whether to use SSL on this connection.
        encoding: Optional; sets the encoder to use for encoding messages.
        """
        self.log.debug("Created connection %s to %s" % (repr(self), repr(address)))

        #: Hooks for events that may occur. Available hooks are:
        #: received, connected, connect_failed, error, hangup
        self.hooks = Hooks(['received', 'connected', 'connect_failed', 'error', 'hangup'])

        #: Stores the address (host and port) to connect to.
        self.address = address
        #: Stores the socket that this IRCConnection uses to communicate with an IRC server.
        self.socket = None

        #: Uses the Dispatcher to route messages to our parent network.
        self.dispatch = Sync(self.hooks.received)

        #: Indicates whether the connection is SSL secured.
        self.ssl = ssl
        # Use the provided encoder, or create a sane one if it is omitted
        self.encoder = encoder if encoder else IRCEncoder('iso-8859-1')

        self.sequence = 0 # sequence numbers
        
        self.ping = None # Stores the ping value sent to the server.
        self.ping_task = None # Stores the ping timeout task so it can be cancelled
        self.random = random.Random() # Generates random values for pings

        self.conn_task = None

    def get_random(self):
        """
        Gets a random hexadecimal string suitable for sending in a PING message.
        """
        return u"%08X" % self.random.getrandbits(32)

    def __repr__(self):
        #mod = self.__class__.__module__
        #cls = self.__class__.__name__
        #return '<%s.%s object "%s">' % (mod, cls, humanid(self))
        return hrepr(self)
    
    def send_ping(self):
        if self.socket is not None:
            # Check results of previous ping (if any)
            if self.ping is not None:
                self.socket.send_now(IRCMessage(self.encoder, None, u'QUIT', ("No ping response for 2 minutes",)))
                self.reconnect()
                return
            # Now send the next ping
            self.ping = self.get_random()
            self.socket.send_now(IRCMessage(self.encoder, None, u'PING', (self.ping,)))
        elif self.ping_task:
            # If there is no connection, cancel ping task
            self.ping_task.cancel()

    def connect(self, force_ipv4=False, depth=0):
        """
        Returns immediately. Will attempt to initiate a connection to the IRC server,
        retrying until successful or a certain number of tries has been exceeded.
        Will try multiple addresses if provided. Returns after successfully
        initiating connection without waiting for the connection to actually
        complete successfully.

        Internally, this function works as follows:

        A nonblocking socket is opened to the server's IP address and port (DNS
        lookups are performed separately). connect() is called, and then the
        IRCConnection passes itself to the IRCSocketManager and returns.

        The IRCSocketManager adds the socket to a list of connecting sockets, and
        passes the socket in to the write and error lists of select(). If the
        socket comes out in write, the IRCConnection's on_connected() is called;
        if the socket comes out in error, on_error() is called and the
        IRCConnection proceeds with automatic reconnection.
        """
        

        # Create new BufferedSocket (wraps real socket)
        self.log.debug("Address acquired, creating socket: " + repr(self.address))
        self.socket = SendThrottledSocket(socket.AF_INET6 if ':' in self.address[0] else socket.AF_INET, parent=self, use_ssl=self.ssl)

        if not self.socket.connect(self.address):
            log.info("Error connecting to %s: %s" % (self.address, self.socket.error))
            self.on_connect_failed()
            self.socket = None
            return
        else:
            # Register our connected socket with the socket manager
            IRCSocketManager.register(self.socket)
            self.log.debug("%s successfully began connection to %s" % (repr(self), self.address))

        # Start task to ping server every 3 minutes
        self.ping_task = SyncRepeating(self.send_ping, 180)()
        
        #assert self.socket is not None

    def disconnect(self):
        """
        Closes the socket without notification to the server. Will not reconnect.
        
        This should only be used when the IRC server is not responding; the proper
        way to disconnect is to send a QUIT command and wait for the server to
        close the connection for us.
        """
        if self.ping_task: self.ping_task.cancel()
        if self.conn_task: self.conn_task.cancel()
        if self.socket:
            self.socket.disconnect()
            self.socket = None
            self.log.debug("Successfully disconnected from %s" % repr(self.address))

    def reconnect(self):
        """
        Convenience method. Calls disconnect() followed by connect().
        """
        self.disconnect()
        self.connect()

    def starttls_begin(self):
        # Send STARTTLS command to server and suspend further writes
        self.socket.suspend('STARTTLS\r\n')

    def starttls_abort(self):
        # Resume writes
        self.socket.resume()

    def starttls_complete(self):
        # initiate SSL handshake and switch socket to SSL mode
        self.socket.starttls()
        self.ssl = True

    def on_connected(self):
        """
        Called by the IRCSocketManager when the socket has successfully connected,
        as indicated by select().
        """
        self.log.debug('Got connection callback for %s' % self.socket)

        self.hooks.connected()


    def on_connect_failed(self):
        """
        Called by the IRCSocketManager if the socket failed to connect.
        """
        self.log.info('Failed to connect to %s with error %s, will retry in 10 seconds' % (self.address, self.socket.error))
        # Retry with a new address after 10 seconds
        #AsyncDelayed(self.connect, 10)()
        self.hooks.connect_failed(self)

    def on_error(self):
        """
        Called by the IRCSocketManager when the socket is in error. The socket will
        be closed and a new connection attempt made.
        """
        self.log.info('Network error: disconnected from %s' % (self.address,))
        # Inform upstream Network of error
        self.hooks.error()
        self.socket = None
        #AsyncDelayed(self.connect, 10)()

    def on_hangup(self):
        """
        Called by the IRCSocketManager after the socket was closed cleanly by the server.
        The server should have sent an ERROR reply with the reason for disconnecting;
        we will decide whether or not to reconnect based on the reason.
        """
        self.log.info('%s has disconnected us.' % (self.address[0],))
        self.hooks.hangup()
        self.socket = None
        #AsyncDelayed(self.connect, 10)()

    def on_received(self):
        """
        Called by the IRCSocketManager when any data is received on the socket.
        """
        if self.socket:
            for line in self.socket.readlines(): 
                # Obtain an IRCMessage representation of this line
                msg = self.process(line)

                # Send this message to our parent IRCNetwork (via the Dispatcher) for further processing
                # This moves processing to another thread ASAP so the SocketManager is not interrupted
                if msg:
                    # Get rid of the MessageReceivedEvent and all its IRCNetwork special-casing
                    # in favor of the much simpler Sync method:
                    self.dispatch(msg)
                    #events.MessageReceivedEvent(self.network, msg).dispatch()
        else:
            self.log.debug("WARNING: on_received() called when socket was None!")

    def process(self, line):
        """
        Processes a single line received from the IRC server.
        """

        # From RFC2812 [page 5]: 
        #>  The prefix, command, and all parameters are separated
        #>  by one ASCII space character (0x20) each.
        # So we split on single spaces. Multiple spaces will end up delimiting empty parameters.
        # Also note that RFC2812 specifies a maximum of 15 parameters.
        def ssplit(text): return filter(lambda x: x, text.split(u' ', 15))

        # Split up into sender, prefix and params
        match = reline.match(line)
        if not match:
            self.log.debug("Received garbage string: %s" % repr(line))
            return
        sender, msgtype, params = match.groups()

        # Respond immediately to a PING message and don't notify downstream
        if msgtype=='PING':
            self.socket.send_now('PONG %s' % params)
            return
        # Filter out PONG messages that are in response to our own PINGs and don't notify downstream
        elif msgtype=='PONG' and self.ping in params:
            # we aren't too picky about which param contains the ping token as long as it's there
            self.ping = None
            return

        # Further split parameters and decode character encodings
        if params:
            # Split at : delimiter
            params = params.split(' :', 1) if params[0] != ':' else ('', params[1:])
            # Decode initial parameters with specified server encoding, and split params on spaces
            paramlist = self.encoder.decode_server(params[0]).split(u' ', 15)
            if len(params) > 1:
                # Attempt to decode final "message" parameter (if given) as utf-8 or fallback encoding
                paramlist.append( self.encoder.decode_utf8(params[1]) )

        # NOTE that, aside from the potential character encoding, there is nothing special
        # about the final parameter. From RFC2812 [page 7]:
        #>   1) After extracting the parameter list, all parameters are equal
        #>      whether matched by <middle> or <trailing>. <trailing> is just a
        #>      syntactic trick to allow SPACE within the parameter.
        #
        # Therefore, according to the RFC, higher-level code does not need to know or care
        # whether the final parameter was prefixed by ':' or not. Different networks may choose
        # to include or omit this character in cases where the final parameter has no spaces;
        # as an example, here is a raw JOIN message as received from two different networks:
        #
        # A network running UnrealIRCd (a popular IRCd)
        #>          :nick!ident@host JOIN :#channelname
        # Freenode, running ircd-seven
        #>          :nick!ident@host JOIN #channelname
        #
        # So it's important not to treat text after the ':' specially, as some IRC libraries do.
        # If we separated out text after the ':' into a separate paremeter called "message",
        # we would have to process JOIN messages from these two networks differently.
        # Instead, we can simply refer to the final parameter as "message", whether or not
        # it was delimited by a ':' character.

        return IRCMessage(self.encoder, self.encoder.decode_server(sender) if sender else None,
                self.encoder.decode_server(msgtype), paramlist, seq=self.sequence)
        # NOTE: From this point on all string handling must be in unicode
        self.sequence += 1


        #self.log.debug(repr(msg))

    def send(self, message, prio=1):
        """
        Sends an IRCMessage (or a string) to the IRC server with the given priority.

        Normal priority should be used for commands/messages sent in response to
        user input, in order to ensure a timely response to the user in the case
        where background messages are queued for delivery.
        """
        if self.socket is None:
            self.log.warn("Attempted to send to a disconnected IRCConnection")
            return

        if isinstance(message, unicode):
            # Encode appropriately
            message = self.encoder.encode_server(message)
        # If message is not a unicode, just assume it can be converted to str.
        # IRCMessage has defined __str__, so it will be fine.
        self.socket.sendline(message, prio)
    
    def send_lazy(self, message):
        """
        Sends an IRCMessage (or a string) to the IRC server with low priority.

        If messages are being queued, then this command will be queued behind all
        commands of a higher priority.

        This priority should be used for any commands/messages that are sent
        automatically in the background to gather data; i.e. commands where the result
        will be saved for future use, so immediate response is not required. Commands
        sent on-demand in response to user input should use normal priority instead.
        """
        self.send(message, 0)

    def send_urgent(self, message):
        """
        Sends an IRCMessage (or a string) to the IRC server with high priority.

        If send throttling is in effect, this command will be queued ahead of all
        lower-priority commands.
        
        This priority should be used for any vital commands, such as QUIT and
        KICK, that need to be executed ASAP no matter how full the send queue is.
        Use sparingly to ensure urgent commands will not be queued behind earlier
        high-priority commands.
        """
        self.send(message, 2)

# ircstack.network.irc
class IRCMessage(object):
    """
    Encapsulates a raw IRC protocol message.
    """
    def __init__(self, encoder, prefix, command, params, seq=None, conn=None):
        self.prefix = prefix
        self.command = command
        self.params = params

        self.encoder = encoder
        self.conn = conn

        self.seq=seq # sequence number for debugging

    def __repr__(self):
        return "IRCMessage(%s, %s, %s)" % (repr(self.prefix), repr(self.command), repr(self.params))

    def __str__(self):
        "Return a raw IRC message, encoded to bytes for network transmission."
        text = self.encoder.encode_server( (u':%s %s' % (self.prefix, self.command)) if self.prefix else self.command )
        if self.params:
            # Coerce to unicode and eliminate empty strings
            params = filter(len, map(unicode, self.params))
            text += u' '.join([u'']+params[:-1])
            message = self.encoder.encode_utf8(params[-1])
            text = '{0} {1}{2}'.format(text, ':' if ' ' in message else '', message)
        return text.translate(None, '\0\r\n')

    def __unicode__(self):
        "Return a unicode string representation of the raw IRC message."
        text = u':%s %s' % (self.prefix, self.command) if self.prefix else self.command
        if self.params:
            params = filter(len, map(unicode, self.params))
            text += u' '.join([u'']+params[:-1])
            message = params[-1]
            text = u'%s %s%s' % (text, u':' if u' ' in message else u'', message)
        return text.translate({u'\0': None, u'\r': None, u'\n': None})

    def __add__(self, other):
        """
        An IRCMessage will act like its network-encoded string representation when
        a string is appended to it using the addition operator,
        or like its unicode representation when a unicode string is appended.
        """
        # Act like a string when added (e.g. for: msg + '\r\n')
        if isinstance(other, unicode):
            return self.__unicode__() + other
        return self.__str__() + other

    def __radd__(self, other):
        """
        An IRCMessage will act like its network-encoded string representation when
        appended to a string using the addition operator,
        or like its unicode represenation when appended to a unicode string.
        """
        # Act like a string when added
        if isinstance(other, unicode):
            return other + self.__unicode__()
        return other + self.__str__()
    
    @property
    def message(self):
        "Convenience property that returns the final parameter."
        return self.params[-1] if self.params else None

    @property
    def target(self):
        """Convenience property that returns the first parameter, which for numeric replies
        is the intended target of the reply."""
        return self.params[0] if self.params else None

    def send(self, prio=1):
        """
        Sends this message to the IRC server, as long as an IRCConnection was defined
        when this IRCMessage was created. This is largely a convenience function.
        """
        if self.conn:
            self.conn.send(str(self), prio)

class LongIRCMessage(IRCMessage):
    """
    Encapsulates a potentially lengthly IRC message that may need to be split up into multiple parts.

    This is achieved by splitting the final parameter and prepending everything preceding that final
    parameter onto each part. This is generally only suitable for PRIVMSG and NOTICE commands. 

    Note that one must use the send() method of a LongIRCMessage object in order to perform splitting.
    """
    def send(self, prio=1):
        """
        Sends this message to the IRC server, as long as an IRCConnection was defined
        when this IRCMessage was created. A message of excessive length will have its
        final parameter split and sent as several messages. Subsequent messages will
        duplicate all but the final parameter of the initial message;
        """
        if not self.conn: return

        # Get the entire message (encoded) and the length of it
        strmsg = str(self)
        total = len(strmsg)

        if total > 510:
            # Message is too long and will be truncated.

            # can't do any splitting if there are no parameters
            if not self.params: return self.conn.send(strmsg, prio)

            # Get the final param (encoded) by itself
            finalparam = self.encoder.encode_utf8(self.message)

            # Calculate length of non-message part
            prefixlen = total - len(finalparam)
            if prefixlen > 500:
                # not much we can do here
                return self.conn.send(strmsg, prio)

            # "prefix" consists of everything except the final parameter
            prefix = strmsg[:prefixlen]

            # Work out how far into "finalparam" we need to split
            cutoff = 508-prefixlen # leave some leeway

            messages = [] # this will hold the split-up final params to send
            while len(finalparam) > cutoff:
                # Search for a space to wrap at
                index = finalparam.rfind(' ', 0, cutoff)

                # If there was no space found close enough to the cutoff point,
                # just cut at the cutoff point
                if cutoff-index > 32:index=cutoff

                # Split on the space
                split, finalparam = finalparam[:index], finalparam[index:].lstrip(' ')
                
                # Add split-off message to the list.
                # If this looks like an unterminated CTCP, terminate it (fixes long /me)
                messages.append(split+'\001' if bool(split.count('\001')&1) else split)
            # Add what's left to the list
            messages.append(finalparam)

            for m in messages:
                # Send each part of the message, applying the prefix
                self.conn.send(prefix+m, prio)


# ircstack.network.irc
class IRCEncoder(object):
    """
    Encapsulates character encoding settings for an IRC network, and performs
    character encoding/decoding according to those settings.
    """
    def __init__(self, fallback='iso-8859-1', utf8=True, server='iso-8859-1'):
        # Encoding for server protocol. Should almost always be ISO-8859-1.
        try:
            ''.decode(server)
        except LookupError:
            # Invalid encoding provided, default to iso-8859-1
            server='iso-8859-1'
        finally:
            self.server = server

        # Whether to attempt UTF-8 decoding for message text.
        self.utf8 = utf8

        # Fallback encoding to use if UTF-8 decoding fails.
        # If utf8 is false, we always use the fallback encoding.
        try:
            ''.decode(fallback)
        except LookupError:
            # Invalid encoding provided, default to iso-8859-1
            fallback='iso-8859-1'
        finally:
            self.fallback=fallback # What encoding do we fall back to?

    def decode_server(self, text):
        """
        Attempt to decode the given text as a fixed encoding (usually iso-8859-1)
        Used for decoding server messages, which are normally in a standard 8-bit encoding.
        """
        return text.decode(self.server, 'replace')

    def decode_utf8(self, text):
        """
        Attempt to decode the given text as UTF-8, with fallback.
        Used for decoding channel messages (or in fact, any free-text message
        sent as final command parameter after the delimiting ':' character).
        """
        try:
            return text.decode('utf-8', 'strict') if self.utf8 else text.decode(self.fallback, errors='replace')
        except UnicodeDecodeError:
            return text.decode(self.fallback, 'replace')

    def encode_server(self, text):
        return str(text.encode(self.server, 'replace'))

    def encode_utf8(self, text):
        # Encode outgoing message correctly
        if self.utf8:
            return str(text.encode('utf-8', 'replace'))
        else:
            return str(text.encode(self.fallback, 'replace'))


log.debug("Network subsystem initialized.")
