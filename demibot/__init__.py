"""
The demibot package contains modules and classes for running an IRCStack-based
bot. It handles plugins, configuration and local user interface.
"""
from __future__ import absolute_import

import sys
# Python 2.6 was released in 2008 - seriously, why are you not yet using it?
# (2.6 required for string.format support, collections.namedtuple, etc)
assert sys.version_info >= (2, 6), """
Outdated Python version!
This software makes use of Python features and libraries that are only available in Python 2.6 or greater.
"""

# Will break hilariously under Python 3.x (even some of the syntax is invalid)
assert sys.version_info < (3, 0), """
Unsupported Python version!
This software was written for Python 2.x and breaks if run under Python 3. Sorry!
"""

from ircstack.util import get_logger, catch_all, Singleton
log = get_logger()

from ircstack.dispatch import Dispatcher
from ircstack.dispatch.events import EventListener, event_handler
from ircstack.dispatch.async import SyncDelayed
import ircstack.dispatch.events as events
from ircstack.network import IRCSocketManager, NoValidAddressError
from ircstack.protocol.irc import IRCNetwork
from demibot import PluginLoader, console
from demibot.config import load_config

__all__ = ["IRCBot"]

# Let the caller configure logging
#logging.basicConfig(format=logLongFormat, datefmt='%H:%M:%S', level=logging.INFO)

class IRCBot(Singleton, EventListener):
    """
    The DemiBot application.

    This class implements the Demibot IRC bot. Call the run() method to execute it.
    
    Note that IRCBot runs as an interactive frontend, presenting a readline-enabled command
    line interface, allowing it to be controlled using commands. For this reason, IRCBot is
    a singleton. Attempting to instantiate it more than once will return the existing instance
    instead of a new one.
    """
    log = get_logger()

    def __init__(self):

        self.conf = load_config()
        self.networks = {}

        super(IRCBot, self).__init__()

    def run(self):
        ### INITIALIZE SERVICES ###
        # Initialize the interactive console.
        # TODO: Use ncurses
        console.setup()
        try:

            # Start the IRCSocketManager. This needs to be started before any networks can be connected
            IRCSocketManager.start()
            # Start the Dispatcher main loop. This needs to be started early, because it is responsible
            # for executing delayed tasks.
            Dispatcher.start()

            ### INITIALIZE NETWORKS ###

            # TODO: Network manager? Is it required?
            for nconf in self.conf.networks:
                try:
                    net = IRCNetwork(nconf)
                except NoValidAddressError as e:
                    self.log.warning("Failed to connect to %s: %s" % (nconf['name'], e.message))
                else:
                    self.networks[net.name] = net

            # Connect all networks
            ccount = 0
            for network in self.networks.values():
                if network.conf.enabled:
                    try:
                        network.connect()
                    except NoValidAddressError as e:
                        self.log.warning("Failed to connect to %s: Could not connect to any of the provided addresses." % network.name)
                    ccount+=1
                else:
                    self.log.info("Ignoring disabled network %s" % network.name)
            self.log.info("%d networks connected" % ccount)
            
            ### DEBUGGING ###

            def test(msg):
                self.log.info(msg)

            #DelayedTask(test, 8.0)("This is the 8 second debug timer..")

            #plugin = PluginLoader.get_plugin('pingpong')
            #for net in self.networks: net.enabled_plugins.append('pingpong')
            #DelayedTask(plugin.unload, 20.0)()

            # Here is where we would register any built-in events
            #ChannelMessageEvent.subscribe(rocket)

            ### END DEBUGGING ###

            # Infinite loop waiting for ^D
            console.run()

            # If we get here, there was a keyboard interrupt (or other exception) and we should shut down
            self.shutdown()
        finally:
            console.teardown()
            pass

    @event_handler(events.WelcomeEvent)
    def on_welcome(self, event):
        self.log.info('Setting modes for %s...' % event.network.name)
        event.network.mode(event.network.nick, '+B-x')

    def quit_networks(self, reason='Terminated'):
        self.log.info("Sending quit command to IRC networks...")
        for network in self.networks.values():
            if network.conf.enabled:
                try:
                    network.quit(reason)
                except RuntimeError:
                    # Socket not connected
                    pass

    def handle_console(self, command):
        """Handles a console command."""
        self.log.debug("Console: %s" % command)
        params = command.split()
        if params[0].lower() in ('stop', 'quit', 'exit', 'shutdown'):
            exit('Shutdown by console command')
        elif params[0].lower() == 'plugin':
            if len(params) < 2:
                self.log.error('Not enough parameters for "plugin" command')
                return
            if params[1].lower() == 'load':
                if len(params) < 3:
                    self.log.error('Usage: "plugin load <plugin>"')
                else:
                    try:
                        PluginLoader.get_plugin(params[2])
                    except:
                        self.log.exception("Unable to load plugin")
            elif params[1].lower() == 'enable':
                if len(params) < 4:
                    self.log.error('Usage: "plugin enable <plugin> <network>"')
                else:
                    if params[3] not in self.networks:
                        self.log.error('No such network')
                        return
                    try:
                        PluginLoader.get_plugin(params[2]).enable(self.networks[params[3]])
                    except:
                        self.log.exception("Unable to load plugin")
            elif params[1].lower() == 'disable':
                if len(params) < 4:
                    self.log.error('Usage: "plugin disable <plugin> <network>"')
                else:
                    if params[3] not in self.networks:
                        self.log.error('No such network')
                        return
                    try:
                        PluginLoader.get_plugin(params[2]).disable(self.networks[params[3]])
                    except:
                        self.log.exception("Unable to find plugin")
            elif params[1].lower() == 'unload':
                if len(params) < 3:
                    self.log.error('Usage: "plugin unload <plugin>"')
                else:
                    if params[2] in PluginLoader.plugins:
                        PluginLoader.plugins[params[2]].unload()
                    else:
                        self.log.error("That plugin is not loaded.")
            elif params[1].lower() == 'reload':
                if len(params) < 3:
                    self.log.error('Usage: "plugin reload <plugin>"')
                else:
                    if params[2] in PluginLoader.plugins:
                        PluginLoader.plugins[params[2]].reload()
                    else:
                        self.log.error("That plugin is not loaded.")
            else:
                self.log.error('Unknown subcommand for "plugin" command')
        else:
            self.log.debug("Dispatching command: %s" % ' '.join(params))
            events.ConsoleCommandEvent(params[0], params[1:]).dispatch()

    def shutdown(self):
        self.log.info('Shutting down IRCBot...')
        IRCSocketManager.shutdown()
        Dispatcher.shutdown()
        self.log.info('Shutdown complete.')

log.debug("IRCBot initialized, loading complete.")
