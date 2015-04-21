import collections

# Library imports
from ircstack.lib import bidict

PrefixMap = bidict.namedbidict('PrefixMap', 'modechars', 'prefixes')

def strlist(l, final='and', conv=str):
    l = map(conv, l)
    return (' %s ' % final).join(filter(len, (', '.join(l[:-1]), l[-1])))

def makedict(value):
    # Python 2.6 doesn't support dict comprehensions
    #return {b[0]:b[1] or None for b in [a.split(':') for a in value.split(',')]}
    return dict((b[0], b[1] or None) for b in [a.split(':') for a in value.split(',')])

class Desc(object):
    def __init__(self, default=None, doc=None):
        self.default = default
        self.val = default
        self.__doc__ = doc

    def __get__(self, obj, objtype=None):
        return self.val

    def __set__(self, obj, newval):
        self.val = newval

    def __delete__(self, obj):
        # Upon deletion, restore default
        self.val = self.default

class ISupport(object):
    def __init__(self):
        # Set defaults for various ISUPPORT parameters

        # CASEMAPPING: 005 default
        self.casemapping = Desc('rfc1459', "String: Defines which uppercase and lowercase characters are equivalent.") # 005 default

        # CHANLIMIT: 005 required. Dict.
        self.chanlimit = Desc({}, """The maximum number of channels that the client may be in.
                Dictionary, keys are channel types, values are integers.""") # 005 required

        # CHANMODES: 005 required; our default is RFC 1459.
        self.chanmodes = Desc(('b', 'k', 'l', 'imnpst'), \
                "Defines categories for channel modes; required to parse MODE messages.")

        # CHANNELLEN: 005/RFC default; None means unlimited
        self.channellen = Desc(200, "Length limit for channel names.")

        # CHANTYPES: 005/RFC default.
        self.chantypes = Desc('#&', "String: Types of channels that are supported.")

        # EXCEPTS: 005 default
        self.excepts = Desc(False, "Are ban exceptions supported, and what modechar? (default +e)")

        # IDCHAN: 005 default is no value.
        self.idchan = Desc({}, '"Safe" channels, see RFC 2811')

        # INVEX: 005 default. Invite exceptions (RFC 2811 4.3.2).
        self.invex = Desc(False, "Are invite exceptions supported, and what modechar? (default +I)")

        # KICKLEN: 005 required
        self.kicklen = Desc(None, "Maximum allowed length for a kick message")

        # MAXLIST: 005 required; default is unknown (no limit)
        self.maxlist = Desc({}, "Maximum length of ban, except, invex etc lists on a channel.")

        # MODES: 005/RFC default. None means unlimited.
        self.modes = Desc(3, 'Maximum number of "variable" modes that may be set at once. i.e. +ooo foo bar baz')

        # NETWORK: No default
        self.network = Desc(None, "Display name for the network. None will use name from config.")

        # NICKLEN: 005/RFC default; numeric value required
        self.nicklen = Desc(9, "Maximum length that may be used in a nickname.")

        # PREFIX: 005/RFC default
        self.prefix = Desc(PrefixMap({'o': '@', 'v': '+'}), "Chanmode to prefix mapping, should be an instance of PrefixMap")

        # SAFELIST: 005 default
        self.safelist = Desc(False, "Boolean: If true, /list will not flood us off the server.")

        # STATUSMSG: 005 default (statusmsg not supported). Should be a sequence of available prefix characters.
        self.statusmsg = Desc('', """Can notices be send to only users with a specific channel status?
                Should be a sequence of available prefix characters.""")

        # TARGMAX: 005 default (no commands support multiple targets). Keys are uppercase commands. Values are limits or None for unlimited.
        self.targmax = Desc({},"""Lists commands that may be given multiple targets, and their limits if any.
                Keys are uppercase commands. Values are the limit as int, or None if unlimited.""") 

        # TOPICLEN: 005 required.
        self.topiclen = Desc(None, "Maximum length for channel topic.")

    def from_param(self, param, value=None):
        """Accepts a parameter and a value to process."""
        
        # Required parameters according to isupport-03:
        if param==u'CHANLIMIT':
            self.chanlimit = makedict(value)
        
        elif param==u'CHANMODES':
            self.chanmodes = value.split(',')

        elif param==u'KICKLEN':
            self.kicklen = int(value) if value else None

        elif param==u'MAXLIST':
            self.maxlist = makedict(value)

        # param STD is unused as there is no ISUPPORT standard yet.

        elif param==u'TOPICLEN':
            self.topiclen = int(value) if value else None

        # Optional parameters according to isupport-03:

        elif param==u'PREFIX':
            self.prefix = PrefixMap(zip(*value[1:].split(')')))

        elif param==u'MODES':
            self.modes = int(value) if value else None

        elif param==u'NETWORK':
            self.network = value

        # Ignore further parameters

        
