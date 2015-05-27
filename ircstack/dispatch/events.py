# Python imports
import sys
import weakref
import traceback
import types
from collections import namedtuple

# Set up logging
from ircstack.util import get_logger, hrepr
log = get_logger()

# Local imports
from . import Dispatcher
from ircstack.dispatch.async import Async

# NOTE: ircstack.events is a low-level import and should
# avoid importing any high-level ircstack modules.

#: Stores information on an Event handler. Used by the EventListener class.
HandlerInfo = namedtuple('HandlerInfo', ('event', 'origin', 'params'))

def event_handler(event_class, **params):
    """
    Decorator to register a method as a handler for a particular type of IRCEvent.

    In order to use this decorator successfully on instance methods, your class must inherit
    Dispatcher.EventListener and call the base constructor otherwise no event handlers will
    be registered.
    """

    __note_to_self = """
    This code:

    >>> @event_handler(EventClass, parameter)
    ... def func():
    ...     pass

    is directly equivalent to:

    >>> def func():
    ...     pass
    >>> register = event_handler(EventClass, parameter)
    >>> func = register(func)
    """
    import inspect
    
    def register(func):
        """
        Subscribes the provided function to the event specified in the decorator, and
        then returns, unmodified, the provided function.
        """
        # Use dark magic to determine the context the function was declared in.
        # Will be "<module>" if declared at module level; will be the class name if declared in a class.
        context = inspect.currentframe().f_back.f_code.co_name

        if context != "<module>":
            # This function is an unbound instance method, act accordingly
            # (note that we receive the actual function, not an unbound method wrapper)
            print "@Dispatcher.event_handler(%s) detected on %s.%s() instance method (%s)" % \
                    (event_class.__name__, context, func.__name__, repr(func))
            #log.warn('@Dispatcher.event_handler() was used on an "%s" instance method, ignoring!' % context)

            # store handler information for later use
            func.handler = HandlerInfo(event=event_class, origin=context, params=params)
        else:
            # Just a normal function
            log.debug('@Dispatcher.event_handler() decorator is subscribing %s to %s' % (repr(func), event_class.__name__))

            # Call this event's subscribe method
            event_class.subscribe(func, params)
        # Return the function, unmodified
        return func
    return register

class WeakBinding(weakref.ref):
    """
    Binds a function to an instance using a weak reference.

    This class serves a similar purpose to the built-in instancemethod type,
    but uses a weak reference in order to avoid reference loops.

    If called when the instance that it is bound to no longer exists, it will
    raise a ReferenceError.
    """
    def __init__(self, instance, func):
        self.__name__ = func.__name__
        self.__doc__ = func.__doc__
        self.im_func = func
        self.im_class = instance.__class__
        super(WeakBinding, self).__init__(instance)

    # Maintain compatibility with bound-method objects
    @property
    def im_self(self):
       return super(WeakBinding, self).__call__()

    def __call__(self, *args, **kwargs):
        instance = self.im_self
        if instance is not None:
            return self.im_func(instance, *args, **kwargs)
        else:
            raise ReferenceError("Cannot call method {0}.{1} of an instance which no longer exists".format(
                self.__name__, self.im_class.__name__))

    def __repr__(self):
        return"<weakly-bound method {1}.{0} of {2}>".format(self.__name__, self.im_class.__name__, repr(self.im_self))


class EventListener(object):
    """
    Base class for classes that wish to use the @event_handler decorator on instance methods.
    """
    log = get_logger()
    def __init__(self):
        self.subscribe_all()

    def subscribe_all(self):
        """
        Subscribes every event handler defined in this class to its corresponding event.

        This method searches the class for functions that were marked by @event_handler when the
        class was declared. It then uses the information stored in the attached HandlerInfo
        to subscribe this instance's corresponding instance method to the correct event.
        """
        self._bound_handlers = {}

        # Locate the class functions that were marked by @event_handler
        handlers = tuple(f for f in self.__class__.__dict__.itervalues() if type(f) is types.FunctionType and hasattr(f, 'handler'))

        for func in handlers:
            event = func.handler.event

            # Bind func to instance before submitting to subscribe().
            #boundmethod = func.__get__(self, self.__class__)
            # Rather than using a Python 2.x binding, make a wrapper that uses a weakref.
            # This avoids cyclic references.
            method = WeakBinding(self, func)

            # The event system stores weakrefs, so we need to store a strong reference
            # to our method wrapper in order to keep it alive so it isn't garbage collected.
            self._bound_handlers[func] = method

            self.log.debug("(EventListener) Subscribing {0} to {1}".format(repr(method), event.__name__))
            # Subscribe the bound method to the event.
            event.subscribe(method, func.handler.params)

    def unsubscribe_all(self):
        """
        Unsubscribes every event handler defined in this class.

        Calls <EventType>.unsubscribe() on the list of handlers saved by the subscribe_all() method.
        If the handlers were already unsubscribed, the list will be empty.
        """
        for func, handler in self._bound_handlers.iteritems():
            func.handler.event.unsubscribe(handler)
        self._bound_handlers = {}

class Sender(object):
    """
    Represents the sender of an IRCMessage.
    
    Attributes:
        nick    The nickname portion of the sender, or None if the message had no prefix.
        ident   The ident portion of the sender, or None if not specified.
        host    The hostname portion of the sender, or None if not specified.
    """
    # TODO: Move this out of the event class, maybe? Why is it in here?
    def __init__(self, message):
        # Save the IRCEncoder from the message for use in __str__
        self.encoder = message.encoder
        self.ident, self.host = None, None

        # Prefix is optional in the IRC protocol.
        # If it is omitted, we are to assume it is from the server we are connected to (as per RFC)
        if message.prefix is None:
            self.nick = None
        else:
            # Extract nickname portion
            self.nick = message.prefix.split('!')[0]

            if '!' in message.prefix:
                try:
                    self.ident, self.host = message.prefix.split('!', 1)[1].split('@', 1)
                except IndexError:
                    pass

    def __str__(self):
        return self.encoder.encode_server(self.__unicode__())

    def __unicode__(self):
        return self.nick if self.ident is None else u"%s!%s@%s" % (self.nick, self.ident, self.host)


##############
# EVENT BASE #
##############

class EventType(type):
    """
    Events use a class-wide WeakKeyDictionary to store their list of subscribers. However,
    if the "subscribers" attribute is initialized in the EventBase class, then a single
    WeakKeyDictionary instance is shared among all classes that inherit EventBase, unless
    the subclasses explicitly define their own, which is not desirable (we want to hide
    all the subscription mechanics in the base class).

    This metaclass initializes the "subscribers" class attribute individually for every
    EventBase subclass, in order to defeat inheritance of the attributes and ensure that
    every class that inherits EventBase gets its own individual WeakKeyDictionary instance.
    """
    def __init__(cls, name, bases, d):
        cls.subscribers = weakref.WeakKeyDictionary()
        super(EventType, cls).__init__(name, bases, d)

class EventBase(object):
    __metaclass__ = EventType # more magic
    """
    Base class for all events. All other Event classes should inherit from this one, and
    specify properties as appropriate.
    """  
    log = get_logger()

    # Allows .dispatch() to be called on an Event in order to submit it to the Dispatcher.
    dispatch = Dispatcher.dispatch

    def __init__(self, dispatch=False): 
        if dispatch:
            # Dispatch this event immediately on creation
            Dispatcher.dispatch(self)
    
    def __call__(self):
        """Invokes the subscribed handlers using the Dispatcher thread pool."""
        # Call each subscribed handler, subject to parameters if any.

        # Danger of dict changing size if a handler gets garbage collected
        # while we are still iterating over the subscribers list. We solve this
        # by storing the list of strong references while iterating.
        refs = self.__class__.subscribers.items()
        for func, params in self.__class__.subscribers.iteritems():
            if not func:
                self.log.warn("Dead weakref detected!")
                continue
            elif self._filter(params):
                if 'async' in params and params['async']:
                    Async(func)(self)
                else:
                    func(self)
        del refs

    def _filter(self, params):
        """
        Internal method. Calls filter() for the calling class. If filter() does not exist
        or returns None, then follow the Method Resolution Order until a base class returns
        a value that is not None, and return that value.
        """
        topcls = type(self)

        # Travel down the MRO
        for cls in type(self).__mro__:
            if 'filter' in cls.__dict__:
                # If this class defines filter(), call it and see what it returns
                value = cls.filter(self, params)
                if value is not None:
                    return value
        # We should never arrive here (because our default filter() returns True)
        # but if we somehow do, return True.
        return True
            

    def filter(self, params):
        """
        When this event is fired, this method is called once per subscribed handler when an
        event is fired, and determines if the handler should be called for this event (or
        whether the decision should be left up to base classes). The default behaviour is
        to call the handler.

        This method takes one argument, which is the dict of parameters which the handler
        specified when it subscribed to this event. This is provided for convenience, but
        you may choose whether to call the handler based on any conditions you choose.

        Subclasses should override this method as appropriate. Your filter() method should
        return one of three values:
        
        False: Do not call the handler, regardless of what superclasses think. You should
            return this if the event content is not relevant to your Event, or if the
            handler gave parameters that exclude this event.

        None: Let superclasses decide if the handler should be called. You should return
            this if the parameters you care about allow the handler to be called. This
            gives superclasses the chance to cancel the event based on other parameters.

        True: Call the handler, regardless of what superclasses say. BEWARE: You should only
            return True if you know exactly what all of your base classes are doing in their
            may have unintended side effects (such as plugins receiving the events for all
            networks, including those they weren't loaded for). Consider returning None
            instead, unless you have a very good reason to use this.

        Notes:

          * If you do not declare a filter() method, it is equivalent to declaring one that
            always returns None (because the superclass method will run instead).

          * You do not need to explicitly call super().filter() unless you are performing
            complicated logic that needs to check whether a base class would call the
            handler. In most cases, simply returning None is sufficient.

          * Do NOT try to use this method to run code when your event is fired. Remember,
            filter() is not only called for each and every handler subscribed to THIS event,
            but is potentially also called multiple times whenever a derived event is fired!
        """
        return True

    @classmethod
    def subscribe(cls, func, params={}):
        """
        Subscribes a handler function to be called when this event is fired.

        Events maintain only a weak reference to the provided handler, to allow modules
        to be garbage collected. This creates a caveat: Subscribing a bound method will
        often fail, because the bound-method object is a transient wrapper around the
        class function and the subscription will lapse when it falls out of scope.
        """
        # cls.subscribers is a WeakKeyDictionary, so that plugin modules
        # may be garbage collected when unloaded.

        # Store a COPY of the params dict because for some reason the same
        # params dict object gets reused for every subscription
        cls.subscribers[func] = params.copy()
        
        # Check if our subscriber comes from a plugin. If so, make a record of its subscription.
        module = sys.modules.get(func.__module__)
        if hasattr(module, '__plugin__'):
            module.__plugin__.subscriptions[func] = cls

    @classmethod
    def unsubscribe(cls, func):
        """Remove func as a subscriber of this particular type of Event."""
        if func in cls.subscribers:
            del cls.subscribers[func]
        module = sys.modules.get(func.__module__)
        if hasattr(module, '__plugin__'):
            del module.__plugin__.subscriptions[func]

##################
### BOT EVENTS ###
##################

class MessageReceivedEvent(EventBase):
    """
    This event is fired when an IRCConnection receives any message (excluding automatic PING/PONG).
    It is designed to be received by an IRCNetwork and is used to offload the processing
    of an incoming message from the SocketManager thread to a Dispatcher thread.
    """
    # TODO: Nuke this entire class from orbit

    def __init__(self, network, message):
        self.network = network
        self.message = message

        super(MessageReceivedEvent, self).__init__()

    @classmethod
    def subscribe(cls, func, params={}):
        """Add func as a subscriber to this event."""
        log.debug("%s has subscribed to MessageReceivedEvent" % func)

        # If the subscribing func is a method of an IRCNetwork:
        if hasattr(func, 'im_self') and func.im_class.__name__.endswith('IRCNetwork'):
            # Store which instance it is in the params
            params['_network'] = func.im_self
        return super(MessageReceivedEvent, cls).subscribe(func, params)

        ## hey this event isn't as bad as I remember, I forgot that I'd already
        ## ripped out the awful special-case code and replaced it with
        ## a nice tidy filter() and parameter system

    def filter(self, params):
        """Filter out networks that are not the network that fired this event."""
        ## EXCEPT FOR THE FACT THAT WE NEED TO FILTER OUT NETWORKS AT ALL, I MEAN REALLY?
        ## Events are just the wrong tool for this job, the Sync() method is so much simpler

        # No need to return None because we inherit directly from EventBase,
        # whose filter() always returns True.
        if '_network' not in params:
            self.log.debug('No _network param found!')
        return '_network' not in params or params['_network'] is self.network

class PluginEvent(EventBase):
    """Base class for plugin-related events."""
    def __init__(self, plugin):
        self.plugin = plugin
        super(PluginEvent, self).__init__()

class PluginLoadEvent(PluginEvent):
    """Fired when a plugin is loaded successfully."""
    pass

class PluginUnloadEvent(PluginEvent):
    """Fired when a plugin is unloaded successfully."""
    pass

class ConsoleCommandEvent(EventBase):
    def __init__(self, command, params):
        self.command = command
        self.params = params
        super(ConsoleCommandEvent, self).__init__()

    def filter(self, params):
        if 'command' in params:
            return params['command'] == self.command and None

##################
### IRC EVENTS ###
##################

class IRCEvent(EventBase):
    """
    An IRCEvent is fired when ANY command or numeric reply is received from an IRC
    network (excluding PING requests, which are handled transparently at a lower
    level). It encapsulates the message data and the IRCNetwork that received the
    message.

    The IRCEvent also serves as the base class for all other IRC events.
    """

    def __init__(self, network, message, dispatch=False):
        """
        Creates an IRCEvent from a message and (by default) automatically dispatches the event
        to any subscribers via the Dispatcher.

        If you wish to create an event without dispatching it, supply the dispatch=False parameter.
        
        network: The network that received the message which triggered this event.
        message: The message that is triggering this event.
        dispatch: If True, the event will be dispatched automatically upon creation.
        """
        # Store the underlying IRCMessage
        self.ircmessage = message
        # Extract and store the sender of this message
        self._sender = Sender(message) if message.prefix else None

        # Store the network that created this event
        self.network = network

        # Base class constructor will dispatch event if asked
        super(IRCEvent, self).__init__(dispatch)

    def filter(self, params):
        """
        Checks whether a handler for this IRCEvent (or subclass) should be
        called for this particular event, based on whether its parent plugin
        is enabled for the network that fired it.
        """
        # This filter will always call a non-plugin handler. For plugins it
        # will only call the handler if the network that fired this event
        # has the plugin enabled.
        ## TODO: eeeeuuurrgh fix it, fix it, fix it, fix it!! fix it, fix it, fix it!

        # No need to return None because we inherit directly from EventBase,
        # whose filter() always returns True.
        return '_plugin_name' not in params or params['_plugin_name'] in self.network.enabled_plugins.keys()

    @classmethod
    def subscribe(cls, func, params={}):
        """Add func as a subscriber to this particular type of IRCEvent."""
        # Save name of subscribing plugin (if plugin) for later use
        if hasattr(sys.modules[func.__module__], '__plugin__'):
            params['_plugin_name'] = sys.modules[func.__module__].__plugin__
        super(IRCEvent, cls).subscribe(func, params)

    @property
    def sender(self):
        """Gets the sender of this message, as included in the message."""
        return self._sender

    @property
    def params(self):
        """Gets the list of parameters included in the IRCMessage."""
        return self.ircmessage.params

    @property
    def message(self):
        """Convenience property that fetches the final parameter."""
        return self.ircmessage.params[-1] if len(self.ircmessage.params) else None

    @property
    def msgtype(self):
        """Gets the IRC command or three-digit reply code."""
        return self.ircmessage.command

class UnknownMessageEvent(IRCEvent):
    """
    This event occurs when a type of message is received that ircstack does not know how to handle.
    """
    pass

class PrivmsgEvent(IRCEvent):
    """
    This event occurs when any PRIVMSG is received.
    
    NOTE: Not to be confused with PrivateMessageEvent, which is raised only when a
    PRIVMSG is specifically addressed to us (i.e. was a private, not a public message).
    """

    @property
    def target(self):
        """Gets the intended recipient of this message."""
        # The first parameter is the recipient of the message - usually this is our own nick,
        # but in a PRIVMSG sent to a channel, it will be the channel name.
        # With the STATUSMSG extension, it could be a channel name prefixed with a PREFIX char,
        # for example "@#channel" for an ops-only message.
        return self.ircmessage.params[0]

    def reply(self, msg):
        """Reply to this message with a message of our own."""
        self.network.privmsg(self.target if '#' in self.target else self.sender.nick, msg)

class CommandEvent(PrivmsgEvent):
    """Raised when a PRIVMSG is received that begins with the bot's command character.
    When registering for this event, you can specify the particular command you want to listen for."""
    pass

    def filter(self, params):
        if 'command' in params:
            return self.ircmessage.message[1:].startswith(params['command']) and None

    @property
    def command(self):
        """Returns the command used, not including the prefix character."""
        return self.ircmessage.message.split(None, 1)[0][1:]

    @property
    def message(self):
        """Returns the content of the command."""
        split = self.ircmessage.message.split(None, 1)
        return split[1] if len(split)>1 else None

class PrivateMessageEvent(PrivmsgEvent):
    """Raised only when a PRIVMSG is received that is addressed to us specifically.
    
    NOTE: Not to be confused with PrivmsgEvent, which is raised for all PRIVMSGs."""
    pass

class ChannelMessageEvent(PrivmsgEvent):
    """Raised only when a PRIVMSG is received that is addressed to a channel we are in."""

    @property
    def channel(self):
        """Gets the channel that this message was sent to."""
        # TODO: Properly support STATUSMSG extension
        # TODO: Support other channel types
        # Ghetto STATUSMSG support: Discard any leading characters before the # of the channel name
        return '#'+self.target.split('#')[1]

class NoticeEvent(PrivmsgEvent):
    """Occurs when a NOTICE is received."""
    pass

class JoinEvent(IRCEvent):
    """Occurs when someone joins a channel we are a member of.
    Also sent as confirmation that we have joined a channel."""
    @property
    def channel(self):
        """Gets the channel that was joined."""
        return self.message

class QuitEvent(IRCEvent):
    """Occurs when someone in one of our channels disconnects from IRC.
    Depending on the IRCd, may also be sent when we quit (but this is nonstandard)."""
    # Quit message contained in IRCEvent.message property
    pass

class NickEvent(IRCEvent):
    """Occurs when a nickname change is observed.
    This may be our own nickname, or someone else's."""
    pass

class EndOfMOTDEvent(IRCEvent):
    """Raised when an End of MOTD reply is received from the server.
    This generally indicates that login to the server is complete."""
    pass

class WelcomeEvent(IRCEvent):
    """Raised when RPL_WELCOME is received.
    Indicates successful login to the IRC server."""
    pass

log.debug("Event Framework initialized.")
