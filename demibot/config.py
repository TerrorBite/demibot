"""
Bottle Configuration module

Reads bot configuration from an outside source in order to obtain settings.
"""
import os
from ircstack.util import get_logger
log = get_logger(__name__)

try:
    import yaml
except ImportError:
    raise SystemExit("""Can't import pyYAML. Ask your administrator to "pip install pyyaml".\nBottle cannot continue.""")

from os import path

class YAMLConfigObject(yaml.YAMLObject):
    """This subclass of YAMLObject defines an object whose constructor is called
    with kwargs when being deserialized from YAML, instead of the constructor
    being skipped and attributes being set directly. This means that the
    YAMLConfigObject can specify default values for unset attributes by using
    default values for constructor arguments."""
    @classmethod
    def from_yaml(cls, loader, node):
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
    yaml_tag = u'!BotConfig'
    __comments__ = {
            'debug': "Sets whether the bot should output debugging information while running.",
            'plugin_blacklist': """Modules in this list are blacklisted completely. They will never
            be loaded, even if specified in a network config file.""",
            }

    def __init__(self, debug=False, plugin_blacklist=[], throttle={'rate': 10.0, 'per': 6.0}, **kwargs):
        self.debug = debug
        self.plugin_blacklist = plugin_blacklist
        self.throttle = throttle
        for k in kwargs:
            log.warn('Unknown setting "%s" found in bottle.yml, consider deleting it.' % k)

class NetworkConfig(YAMLConfigObject):
    yaml_tag='!NetworkConfig'

    def __init__(self, name='Network', enabled=False, nick=['Bottle', 'Bottle1', 'Bottle2'], ident='bottle',
            realname='"Bottle" IRC bot', join_on_invite=False, rejoin_on_kick=None, prefix_char='!',
            servers=[], channels={}, plugins=[], **kwargs):
        self.name = name
        self.enabled = enabled
        self.nick = nick
        self.ident = ident
        self.realname = realname
        self.join_on_invite = join_on_invite
        self.rejoin_on_kick = rejoin_on_kick
        self.prefix_char = prefix_char
        self.servers = servers
        self.plugins = plugins

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
    __slots__ = ['key', 'autojoin', 'join_on_invite', 'rejoin_on_kick'] 
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
