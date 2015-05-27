"""
DemiBot configuration module.

Reads bot configuration from an outside source in order to obtain settings.
"""
import os
from ircstack.util import get_logger
log = get_logger(__name__)

try:
    import yaml
except ImportError:
    raise SystemExit("""Can't import pyYAML. Ask your administrator to "pip install pyyaml".\nDemiBot cannot continue.""")

from os import path

class YAMLConfigObject(yaml.YAMLObject):
    """
    Normally a YAMLObject is deserialized by creating an instance without
    calling the constructor, then directly setting the instance attributes.
    This avoids any possible side effects that may occur by calling the
    constructor, and makes sense for "normal" objects that perform specific
    initialization when instantiated.
    
    This subclass of YAMLObject defines an object whose constructor is called
    with kwargs when being deserialized from YAML. This makes it easier to create
    a class that only stores data, with the caveat that the class must expect a
    constructor call when being deserialized.
    
    By using this method, the YAMLConfigObject can specify default values for
    unset attributes by using default values for constructor arguments.
    """

    #TODO: There has to be a better way. Try storing default values as class attributes?
    # That may require also overloading to_yaml() to make sure only the instance
    # attributes are re-serialized, and not the default-value class attributes.
    @classmethod
    def from_yaml(cls, loader, node):
        """
        Call constructor instead of manually setting attributes.
        """
        # Construct deep mapping to ensure all data is present in constructor call
        fields = loader.construct_mapping(node, deep=True)
        return cls(**fields)

def load_config():
    with open('demibot.yml', 'r') as f:
        conf = yaml.load(f)

    def load_network(filename):
        with open(os.path.join('networks',filename), 'r') as f:
            netconf = yaml.load(f)
        return netconf

    # Load network configs: Create a NetworkConfig for each YAML file in the networks folder
    conf.networks = [load_network(filename) for filename in os.listdir('networks') if filename.endswith('.yml')]
    return conf

class BotConfig(YAMLConfigObject):
    """
    Stores global configuration for the :py:class:`demibot.IRCBot`.

    This class is intended to be instantiated automatically by PyYAML.
    """
    yaml_tag = u'!BotConfig' #: YAML object tag used for loading. Do not change.

    def __init__(self, debug=False, plugin_blacklist=[], throttle={'rate': 10.0, 'per': 6.0}, **kwargs):
        """
        Creates a new BotConfig object.
        """
        #: Sets whether the bot should output debugging information while running.
        self.debug = debug
        #: Modules named in this sequence are entirely blacklisted from the bot.
        #: They will never be loaded, even if specified in a network config file.
        self.plugin_blacklist = plugin_blacklist
        #: A dict containing default values for send-throttling. By default the bot will
        #: send no more than *rate* messages in *per* seconds.
        self.throttle = throttle
        for k in kwargs:
            log.warn('Unknown setting "%s" found in bot config file, consider deleting it.' % k)

class NetworkConfig(YAMLConfigObject):
    """
    Stores configuration values for an :py:class:`~ircstack.protocol.irc.IRCNetwork`.

    This class is intended to be instantiated automatically by PyYAML.
    """
    yaml_tag=u'!NetworkConfig' #: YAML object tag used for loading. Do not change.

    def __init__(self, name='Network', enabled=False, nick=('DemiBot', 'DemiBot1', 'DemiBot2'), ident='demibot',
            realname='DemiBot instance: http://lethargiclion.net/demibot/', join_on_invite=False,
            rejoin_on_kick=None, prefix_char='!', servers=[], channels={}, plugins={}, **kwargs):
        
        self.name = name #: The friendly name of the network.

        self.enabled = enabled
        """
        Sets whether this IRC network should be used.

        If *False*, the configuration will be ignored and no :py:class:`~ircstack.protocol.irc.IRCNetwork` will be created.
        """

        self.nick = nick
        """
        A sequence of nicknames to use when logging in to the network. If the first nickname is
        unavaliable, the second will be tried, etc. If all are unavailable, the :py:class:`~ircstack.protocol.irc.IRCNetwork` will
        use its own methods to try and create a unique nickname.
        """

        self.ident = ident
        """
        The "ident" to use when logging in to the network. The IRC server will only use this if
        an actual ident reply is not available.
        """

        self.realname = realname
        """
        The "realname" to provide when logging in to the network. This is a string that is shown
        to other users in a WHOIS reply.
        """

        #: True if the bot should try to join a channel when it receives an INVITE to that channel.
        self.join_on_invite = join_on_invite

        #: True if the bot should try to rejoin a channel automatically if it is KICKed out.
        self.rejoin_on_kick = rejoin_on_kick

        self.prefix_char = prefix_char
        """
        The character that the bot will use to recognise commands directed at it. For example,
        if the prefix character is "!" (the default), the bot will recognise the message "!help"
        as being a "help" command from a user.
        """

        self.servers = servers
        """
        A sequence of server URIs as strings. For example: "irc://irc.freenode.net:6667/"

        For SSL, use the ircs: scheme.
        """

        #: A dict of :py:class:`NetworkPluginConfig` instances, keyed by plugin name. See also:
        #: :py:class:`demibot.PluginLoader.Plugin`
        self.plugins = plugins

        #: A dict of :py:class:`NetworkChannelConfig` instances, keyed by channel name.
        self.channels = {}

        if not channels:
            log.warn("Network %s has no configured channels!" % name)
        for cname, channel in channels.iteritems():
            if isinstance(channel, dict):
                self.channels[cname] = NetworkChannelConfig(**channel)
            else:
                log.warn("Invalid channel settings found for channel %s in network %s, using defaults." % (cname, self.name))
                log.debug("Invalid value for %s was: %s" % (cname, repr(channel)))
                self.channels[cname] = NetworkChannelConfig()
        for k in kwargs:
            log.warn('Unknown setting "%s" found in network config for %s, consider deleting it.' % (name, k))

class NetworkChannelConfig(object):
    """
    Stores configuration for an individual channel within a network.

    This class is intended to be instantiated automatically by PyYAML.

    .. py:data:: key

        Stores the key/password required to join the channel if chanmode +k is set.
        If set to *None*, the bot won't send any key. Defaults to *None*.

    .. py:data:: autojoin

        Sets whether the bot will automatically join the channel on connect. Defaults to *True*.

    .. py:data:: join_on_invite

        Sets whether the bot should automatically join the channel when it receives an INVITE to this channel.
        If set to *None*, the value from the parent NetworkConfig is used. Defaults to *None*.

    .. py:data:: rejoin_on_kick

        Sets whether the bot should try to rejoin automatically if it is KICKed out of this channel.
        If set to *None*, the value from the parent NetworkConfig is used. Defaults to *None*.

    """
    __slots__ = ('key', 'autojoin', 'join_on_invite', 'rejoin_on_kick')
    def __init__(self, key=None, autojoin=True, join_on_invite=None, rejoin_on_kick=None, **kwargs):
        self.key = key
        self.autojoin = autojoin
        self.join_on_invite = join_on_invite
        self.rejoin_on_kick = rejoin_on_kick
        for k in kwargs:
            log.warn('Unknown setting "%s" found in channel config for %s, consider deleting it.' % (key, k))

class NetworkPluginConfig(yaml.YAMLObject):
    def __init__(self):
        self.enabled = True
        self.settings = {}

log.debug("Configuration subsystem initialized.")
