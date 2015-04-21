
# NOTE: ircstack.network should avoid importing from any ircstack packages
# except itself and dispatch.

__all__ = ['IRCConnection', 'IRCSocketManager', 'NoValidAddressError']

# Python imports
import urlparse
import errno
import itertools
import random
import socket
import re
import time
#from collections import deque

# Set up logging
from ircstack.util import get_logger
log = get_logger(__name__)

# ircstack imports
from ircstack.util import hrepr
from ircstack.dispatch.async import DelayedTask, RepeatingTask
import ircstack.dispatch.events as events
from ircstack.network import IRCSocketManager
from ircstack.network.sockets import SendThrottledSocket

# Line matching regex
reline = re.compile(r'(?::([^ ]+) )?([^ ]+)(?: (.*))?$')

# ircstack.network.irc
class IRCConnection(object):
    """
    Represents and handles a connection to an IRC network.

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
    
    def __init__(self, network, serverlist, encoder=None):
        """
        Creates an IRCConnection.
        
        network: The IRCNetwork that owns this connection.
        serverlist: a list of servers to connect to, as a (hostname, port) tuple.
        encoding: Optional; sets a fallback encoding to use if UTF-8 decoding fails.
        utf8: Optional; if false, will always use the fallback encoding instead of UTF-8.
        """
        self.log =  get_logger(self.__class__.__name__)
        self.log.debug("Created %s on %s" % (repr(self), repr(network)))

        self.socket = None
        self.network = network
        # Use the provided encoder, or create a sane one if it is omitted
        self.encoder = encoder if encoder else IRCEncoder('iso-8859-1')

        self.sequence = 0 # sequence numbers
        
        self.ping = None # used for ping timeout detection
        self.ping_task = None
        self.random = random.Random() # generates random values for pings


        #ip4list, ip6list = self.dns_lookup(serverlist)

        # TODO: Configurable option to select IPv6 only / IPv4 only / IPv6 preferred / IPv4 preferred
        self.prefer_ipv4 = False
        self.only_preferred = False

        # For now, just pass in hostnames
        self.servers = itertools.cycle(serverlist)

        # This will hold IP addresses after DNS lookup
        self.addresses = itertools.cycle([])

    def get_random(self):
        """Gets a random hexadecimal string suitable for sending in a PING message."""
        return u"%08X" % self.random.getrandbits(32)

    def dns_lookup(self, serverlist):
        # Get all IP addresses for a hostname or list of hostnames, so we can do proper
        # round-robin addressing and build separate lists of ipv4 and ipv6 addresses
        ip4list, ip6list = [], []
        for server in serverlist:
            self.log.info('Resolving address %s:%d' % server.address) 
            try:
                addrinfo = socket.getaddrinfo(*server.address)
            except socket.error, e:
                if e[0] == errno.ENOENT:
                    # Hostname does not exist
                    self.log.warn('Host not found resolving %s' % server.host)
                    continue
                else:
                    self.log.error('Unknown error %d resolving %s: %s' % e[0], server.host, e[1])
            else:
                # Python 2.6 doesn't support dict comprehensions
                #ip4list += {tuple(x[4]) for x in addrinfo if x[0]==socket.AF_INET}
                #ip6list += {tuple(x[4][:2]) for x in addrinfo if x[0]==socket.AF_INET6}
                ip4list += [tuple(x[4]) for x in addrinfo if x[0]==socket.AF_INET]
                ip6list += [tuple(x[4][:2]) for x in addrinfo if x[0]==socket.AF_INET6]
                log.debug(ip4list)
                log.debug(ip6list)

        if not ip6list and not ip4list: 
            self.log.error('Failed to create IRCConnection: No valid addresses supplied')
            raise NoValidAddressError('No valid addresses supplied')

        return ip4list, ip6list

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

    def connect(self, force_ipv4=False):
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
        while True:
            self.log.debug("Fetching next address")
            try:
                self.current_addr = self.addresses.next()
                break
            except StopIteration:
                # new server required
                self.log.debug("No addresses, fetching next server")
                try:
                    self.current_server = self.servers.next()
                except StopIteration:
                    self.log.error('Connection request failed: No more servers to connect to')
                    self.socket = None
                    return
                else:
                    # Is this server already an IPv4 address?
                    try:
                        socket.inet_pton(socket.AF_INET, self.current_server.host)
                        self.addresses = itertools.cycle([self.current_server.address])
                        continue
                    except socket.error:
                        # it isn't IPv4. It might be IPv6
                        if not force_ipv4:
                            try:
                                socket.inet_pton(socket.AF_INET, self.current_server.host)
                                self.addresses = itertools.cycle([self.current_server.address])
                                continue
                            except socket.error:
                                pass
                        self.log.debug("DNS lookup required")
                        # It's not an IP address, do a DNS lookup
                        ip4list, ip6list = self.dns_lookup([self.current_server])
                        if force_ipv4:
                            self.addresses = itertools.cycle(ip4list)
                        elif self.only_preferred:
                            self.addresses = itertools.cycle(ip4list) if self.prefer_ipv4 else itertools.cycle(ip6list)
                        else:
                            self.addresses = itertools.cycle(ip4list+ip6list) if self.prefer_ipv4 else itertools.cycle(ip6list+ip4list)
                        # jump to top to fetch first address
                        continue

        self.log.debug("Address acquired, creating socket: " + repr(self.current_addr))
        self.socket = SendThrottledSocket(socket.AF_INET6 if ':' in self.current_addr[0] else socket.AF_INET, parent=self, use_ssl=self.current_server.ssl)

        if not self.socket.connect(self.current_addr):
            log.info("Error connecting to %s: %s" % (self.current_addr, self.socket.error))
            # Connection attempt failed. Try next address
            # Recursive call until either connected, or no more addresses
            return self.connect()
        else:
            # Register our connected socket with the socket manager
            IRCSocketManager.register(self.socket)
            self.log.debug("%s successfully began connection to %s" % (repr(self), self.current_addr))

        # Start task to ping server every 3 minutes
        self.ping_task = RepeatingTask(self.send_ping, 180)()
        
        #assert self.socket is not None

    def disconnect(self):
        """
        Closes the socket without notification to the server. Will not reconnect.
        
        This should only be used when the IRC server is not responding; the proper
        way to disconnect is to send a QUIT command and wait for the server to
        close the connection for us.
        """
        if self.ping_task: self.ping_task.cancel()
        if self.socket:
            self.socket.disconnect()
            self.socket = None
            self.log.debug("Successfully disconnected from %s" % repr(self.current_addr))

    def reconnect(self):
        """
        Convenience method. Calls disconnect() followed by connect().
        """
        self.disconnect()
        self.connect()

    def starttls_begin(self):
        self.socket.suspend('STARTTLS\r\n')

    def starttls_abort(self):
        self.socket.resume()

    def starttls_complete(self):
        self.socket.starttls()

    def on_connected(self):
        """
        Called by the IRCSocketManager when the socket has successfully connected,
        as indicated by select().
        """
        self.log.debug('Got connection callback for %s' % self.socket)

        if self.network: self.network.on_connected()


    def on_connect_failed(self):
        """
        Called by the IRCSocketManager if the socket failed to connect.
        """
        self.log.info('Failed to connect to %s with error %s, will retry in 10 seconds' % (self.current_addr, self.socket.error))
        # Retry with a new address after 10 seconds
        DelayedTask(self.connect, 10)()

    def on_error(self):
        """
        Called by the IRCSocketManager when the socket is in error. The socket will
        be closed and a new connection attempt made.
        """
        self.log.info('Network error: disconnected from %s' % (self.current_addr,))
        # TODO: Inform upstream Network of error
        if self.network: self.network.on_disconnected()
        self.socket = None
        DelayedTask(self.connect, 10)()

    def on_hangup(self):
        """
        Called by the IRCSocketManager after the socket was closed cleanly by the server.
        The server should have sent an ERROR reply with the reason for disconnecting;
        we will decide whether or not to reconnect based on the reason.
        """
        # DONE: How does the IRCSocketManager detect this?
        self.log.info('%s has disconnected us.' % (self.current_addr[0],))
        if self.network: self.network.on_disconnected()
        self.socket = None
        DelayedTask(self.connect, 10)()

    def on_received(self):
        """
        Called by the IRCSocketManager when any data is received on the socket.
        """
        if self.socket:
            for line in self.socket.readlines(): 
                # Obtain an IRCMessage representation of this line
                msg = self.process(line)

                # Send this message to the Dispatcher (via our parent IRCNetwork) for further processing
                # This moves processing to another thread ASAP so the SocketManager is not interrupted
                if self.network and msg:
                    events.MessageReceivedEvent(self.network, msg).dispatch()
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

# ircstack.network.irc
class NoValidAddressError(Exception):
    pass

# ircstack.network.irc
class IRCServer(object):
    """Holds details about an IRC server."""
    def __init__(self, uri):
        """Accepts an irc:// (or ircs://) URI."""
        result = urlparse.urlparse(uri, allow_fragments=False)
        self.ssl = True if result.scheme.lower() == 'ircs' else False
        self.host = result.hostname
        self.port = result.port or 994 if result.scheme.lower() == 'ircs' else 6667
        self.password = result.password
        self.ident = result.username

    @property
    def address(self):
        return (self.host, self.port)


log.debug("Network subsystem initialized.")
