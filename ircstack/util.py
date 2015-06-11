# Python imports
import weakref
import sys
import threading
import inspect
import traceback
import logging
import collections
from time import time
from heapq import *

try:
    from weakref import WeakSet
except:
    # Python 2.5 and 2.6
    from ircstack.lib.weakrefset import WeakSet

def get_logger(name=None):
    if name is None:
        frame = inspect.currentframe().f_back
        co_name = frame.f_code.co_name
        if co_name == '<module>':
            name = frame.f_locals['__name__']
        elif '__module__' in frame.f_locals:
            name = '.'.join((frame.f_locals['__module__'], frame.f_code.co_name))
        else:
            try:
                instance = frame.f_locals[frame.f_code.co_varnames[0]]
                name = '.'.join((instance.__class__.__module__,instance.__class__.__name__))
            except (IndexError, KeyError):
                name = '<Unknown>'
        del frame

    import logging
    from ircstack.lib.stacklogger import StackFilter
    log = logging.getLogger(name)
    log.addFilter(StackFilter())
    #log.debug("Guessing name [%s]" % name)
    return log

# Set up logging
log = get_logger()

# Please avoid any ircstack imports in this file

class PriorityQueue(object):
    """
    A simple Priority Queue implementation using a hashtree for efficiency.
    
    The queue operates as a FIFO (first-in, first-out) queue with priority
    override. Items added to the queue are given a priority. Items in the
    queue will automatically jump ahead of all items with a lower priority.
    Items with the same priority as each other will be retrieved in the
    same order in which they were added.

    For simplicity, priority values must be integers (or implement __int__).
    Priority values may be negative.
    """
    def __init__(self):
        self.heap = []

    def __len__(self):
        return len(self.heap)

    def __iter__(self):
        heapcopy = list(self.heap)
        while heapcopy:
            yield heappop(heapcopy)[2]

    def has_items(self):
        """Returns True if the queue contains entries."""
        return bool(self.heap)

    def get_prio(self):
        """
        Returns a tuple containing the item itself and its priority.
        """
        try:
            prio, when, task = heappop(self.heap)
            return (task, -prio)
        except IndexError:
            raise IndexError("The PriorityQueue is empty!")

    def get(self):
        """Return only the item, no information about its priority is returned."""
        return self.get_prio()[0]

    def put(self, task, prio=1):
        heappush(self.heap, (-int(prio), time(), task))

    def peek(self):
        return self.heap[0][2]

class Bunch(object):
    """
    Utility class that implements a C-like structure.
    
    See also: collections.namedtuple, but note that namedtuple is immutable.
    """
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


_required = object()
def namedtuple(typename, field_names, verbose=False, rename=False):
    """
    Wrapper around collections.namedtuple that allows default values to be specified.
    """
    if type(field_names) is dict:
        # Dict keys are field names, values are defaults
        defaults = [x for x in field_names.values() if x is not _required]
        result = collections.namedtuple(typename, field_names.keys(), verbose, rename)
        result.__init__.func_defaults = defaults
        return
    else:
        # Use unmodified collections.namedtuple
        return collections.namedtuple(typename, field_names, verbose, rename)
namedtuple.REQUIRED = _required
del _required

def singleton(cls):
    """
    DEPRECATED: This decorator is a bad idea and a poor implementation.
    To implement a singleton class, please inherit from util.Singleton instead.

    Singleton decorator. Wraps a class, ensuring that the first time the class
    "constructor" (i.e. this wrapper) is called, it returns a new instance; but
    subsequent calls will return the existing instance.
    """
    def getinstance(*args, **kwargs):
        if hasattr(cls, 'instance'):
            return cls.instance
        cls.instance = cls(*args, **kwargs)
        return cls.instance
    # Any class using this decorator will have the class name bound to the above function, not to a class object.
    # This means we won't be able to inherit from this class ("class Derived(ClassName):" will fail).
    # Terrible workaround:
    # We set the "cls" attribute on the function so that you can inherit from ClassName.cls instead.
    getinstance.cls = cls
    return getinstance

class Singleton(object):
    """
    Base class for singletons. Any class that inherits from this one will be a singleton.
    i.e. attempting to instantiate another copy will instead return the existing one.
    If using multiple inheritance, it is advised that this class be inherited first to avoid
    any potential conflicts.
    """
    def __new__(cls, *args, **kwargs):
        # Accept and discard potential arguments to subclass constructor

        # Check if this particular class already has an instance
        if not hasattr(cls, '__instance__'):
            # If it doesn't, then instantiate it
            cls.__instance__ = super(Singleton, cls).__new__(cls)
        else:
            # if it does, replace __init__ with a no-op so that it won't be called again
            cls.__init__ = Singleton._pass
        return cls.__instance__

    @classmethod
    def _pass(self, *args, **kwargs):
        # Accept and discard potential arguments to subclass constructor
        pass

# Hooks system
class Hook(object):
    """
    A basic system that works similar to the way events do in C#.
    """
    def __init__(self, name):
        #self._targets = WeakSet()
        self._targets = set()
        self.name = name

    def __len__(self):
        return len(self._targets)

    def __iadd__(self, other):
        if callable(other):
            self._targets.add(other)
        else:
            raise TypeError("You can only add a callable to a hook.")
        return self

    def __isub__(self, other):
        self._targets.discard(other)
        return self

    def __call__(self, *args, **kwargs):
        #log.debug('Hook({0.name}) was called, targets are {0._targets!r}'.format(self))
        for target in self._targets:
            #log.debug('Target: {0!r}'.format(target))
            if callable(target):
                target(*args, **kwargs)
            else:
                log.debug('Target for Hook() is not callable')

class Hooks(Bunch):
    def __init__(self, names):
        super(Hooks, self).__init__(**dict([(name, Hook(name)) for name in names]))
        #log.debug( repr(dict([(key, getattr(self, key)) for key in dir(self) if not key.startswith('__')])) )

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

    def __nonzero__(self):
        return self.im_self is not None

    def __repr__(self):
        return"<weakly-bound method {1}.{0} of {2}>".format(self.__name__, self.im_class.__name__, repr(self.im_self))

class WeakCallable(weakref.ref):
    def __init__(self, func):
        self.__name__ = func.__name__
        self.__doc__ = func.__doc__
        super(WeakCallable, self).__init__(func)

    @property
    def _target(self):
        return super(WeakCallable, self).__call__()

    def __call__(self, *args, **kwargs):
        instance = _target
        if instance is not None:
            return self._target(*args, **kwargs)
        else:
            raise ReferenceError("Callable {0} no longer exists".format(self.__name__))

    def __nonzero__(self):
        return self._target is not None

    def __repr__(self):
        return"<weak callable {0}>".format(self.__name__)

class catch_all(object):
    """
    Decorator to catch and log exceptions that occur within a method.

    This is intended for use with the run() methods called by threads.
    By wrapping a run() method, exceptions that would have killed the
    thread are caught, logged, and then the run() method is restarted
    after a short delay.

    If the same exception reoccurs enough times, then the wrapper will
    eventually let the thread die.
    """
    def __init__(self, retry, message=None):
        # Store decorator parameters
        self.retry = retry
        self.message = message

    def __call__(self, func):
        # Create wrapper function
        def log_exception(*args, **kwargs):
            # Use defined message if any, else use a default
            message = self.message if self.message else \
                    'Unhandled exception occurred in thread %s' % threading.current_thread().name
            extype = None
            excount = 0
            log = None
            run = True

            # Main loop (used if retry is True)
            while run:
                run = self.retry
                try:
                    # Run the wrapped function until it exits or throws an exception
                    return func(*args, **kwargs)
                except Exception, e:
                    # First check if this exception is the same type as the
                    # last one the wrapped function threw (if any)
                    if type(e)==extype:
                        # If so, increment count
                        excount+=1
                    else:
                        extype = type(e)
                        excount=0

                    # Find the module that the wrapped func is declared in,
                    # so we can extract and use its logger. If it doesn't have
                    # a logger, use ours.
                    func_locals = inspect.trace()[-1][0].f_locals
                    func_log = sys.modules[func.__module__].log if hasattr(func, '__module__') \
                            and hasattr(sys.modules[func.__module__], 'log') else log

                    # Sanity check: If an exception is thrown during interpreter shutdown, it's possible
                    # that the log attribute has been set to None already. If so, just use the logging
                    # module directly, which should still work.
                    func_log = func_log if func_log else logging

                    # Log the exception and a warning message (depending on value of retry param)
                    func_log.error(message, exc_info=True)
                    func_log.debug('Locals: %s' % repr(func_locals))
                    if self.retry is False:
                        func_log.critical('The thread %s will now terminate due to this exception.' % (threading.current_thread().name,))
                    else:
                        func_log.warn('The thread %s will now be relaunched.' % (threading.current_thread().name,))

                # If the same exception type was thrown more than 10 times, then
                # it's highly likely 
                if excount>10:
                    log.critical('The thread %s is throwing exceptions too fast and will now terminate.' % (threading.current_thread().name,))
                    break

        return log_exception

    def ratelimit(self):
        """Rate-limiting function for the catch_all decorator class."""
        rate, per = (5.0, 1.0)
        current = time()
        self._rate_allowance = rate
        self._rate_last



# Each list in the tuple below should contain a PRIME number of entries in order to avoid potential collisions.
_rdata = ([ # 47 physical attributes and emotions
        "Strong", "Dark", "Angry", "Happy", "Ignorant", "Awesome", "Outrageous", "Depressed", 
        "Glorious", "Furious", "Woeful", "Striped", "Short", "Raging", "Huge", "Contented", 
        "Scaly", "Tiny", "Slow", "Creepy", "Ecstatic", "Tall", "Small", "Interesting", "Proud", 
        "Amazing", "Big", "Stunning", "Thin", "Scary", "Pale", "Spotted", "Massive", "Fabulous", 
        "Quick", "Fuzzy", "Powerful", "Great", "Speedy", "Sad", "Hairy", "Smelly", "Melancholy", 
        "Grumpy", "Chubby", "Weak", "Shaggy"
    ],[ # 41 colours and behaviours
        "Yellow", "Blue", "Turquoise", "Sprawling", "Dancing", "Brown", "Green", "Tricky", 
        "Pink", "Emerald", "Cyan", "Sleeping", "Crazy", "Flying", "Red", "Intrepid", "Grey", 
        "Sapphire", "Lethargic", "Purple", "Nosy", "Orange", "Iridescent", "Energetic", "White", 
        "Vermilion", "Black", "Lazy", "Monochrome", "Magenta", "Rainbow", "Curious", "Truthful", 
        "Calm", "Teal", "Galloping", "Fighting", "Boring", "Sneaky", "Bouncing", "Honest"
    ],[ # 113 animal names
        "Rhino", "Ibex", "Heron", "Wolf", "Puppy", "Kingfisher", "Orca", "Sparrow", "Monkey", 
        "Hyrax", "Armadillo", "Leopard", "Parrot", "Unicorn", "Kangaroo", "Rooster", "Ostrich", 
        "Zebra", "Serval", "Flyingfox", "Cow", "Lizard", "Wallaby", "Rat", "Husky", "Elephant", 
        "Serpent", "Mammoth", "Anteater", "Emu", "Fox", "Puma", "Tiger", "Hermitcrab", "Pegasus", 
        "Yak", "Donkey", "Duckling", "Stoat", "Werewolf", "Goldfish", "Gryphon", "Owl", "Frog", 
        "Pony", "Buck", "Elk", "Vole", "Orangutan", "Kestrel", "Bat", "Raven", "Lion", "Harpy", 
        "Llama", "Goat", "Falcon", "Lemming", "Jellyfish", "Squirrel", "Hippo", "Thylacine", 
        "Bobcat", "Meerkat", "Mouse", "Cub", "Possum", "Otter", "Antelope", "Dog", "Gopher", 
        "Chicken", "Naga", "Turtle", "Boar", "Whale", "Koala", "Impala", "Giraffe", "Sphinx", 
        "Hyena", "Ape", "Penguin", "Vixen", "Gorilla", "Hawk", "Lynx", "Wildcat", "Ferret", 
        "Iguana", "Narwhal", "Beaver", "Snake", "Badger", "Numbat", "Newt", "Echidna", "Bear", 
        "Dragon", "Walrus", "Dugong", "Mongoose", "Eagle", "Dolphin", "Vulture", "Aardvark", 
        "Cougar", "Flamingo", "Quoll", "Platypus", "Cheetah", "Joey", "Ocelot", "Toucan"
    ])

def humanid(thing):
    """
    Generates and returns a unique*, human-readable name based on the id() of
    the provided object. Will always return the same name for that object.

    *There are currently almost 218,000 possible names, enough to make it highly
    unlikely that two objects will return the same name.

    Names are composed of two adjectives and an animal (thanks to gfycat.com for
    the inspiration).
    """

    return _readable(id(thing))

def hrepr(thing):
    if isinstance(thing, object):
        return '<%s.%s object "%s">' % (thing.__class__.__module__, thing.__class__.__name__, humanid(thing))
    return repr(thing)

def _readable(num):
    #second = num % nseconds
    #minute = (num/nseconds + num) % nminutes
    #hour = (num/(nseconds*nminutes) + minute) % nhours
    
    nadj1, nadj2, nanimals = [len(x) for x in _rdata]

    animal = num % nanimals
    adj2 = (num/nanimals + animal) % nadj2
    adj1 = (num/(nanimals*nadj2) + adj2 + animal) % nadj1

    #num2 = adj1*nadj2*nanimals + adj2*nanimals + animal

    return "%s%s%s" % (_rdata[0][adj1], _rdata[1][adj2], _rdata[2][animal])

def dbg_write():
    f = file('animalnames.txt', 'w')
    for x in range(217751):
        f.write(_readable(x)+'\n')
    f.close()

#log.debug("Utility Library loaded.")


def _set_proc_name(newname):
    from ctypes import cdll, byref, create_string_buffer
    libc = cdll.LoadLibrary('libc.so.6')
    buff = create_string_buffer(len(newname)+1)
    buff.value = newname
    libc.prctl(15, byref(buff), 0, 0, 0)

def set_thread_name(func):
    def wrapper(*args, **kwargs):
        _set_proc_name(threading.current_thread().name)
        func(*args, **kwargs)
    return wrapper
