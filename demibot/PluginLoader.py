"""
This module implements the plugin loading and unloading functionality of DemiBot.
"""

# Python imports
import os, sys, traceback, imp, weakref
import time

# Set up logging
from ircstack.util import get_logger
log = get_logger(__name__)

# ircstack imports
from ircstack.dispatch.async import Dispatcher, Async
from ircstack.dispatch.events import PluginUnloadEvent, event_handler

#: A dict of currently loaded Plugin instances, keyed by plugin name.
plugins = {}

#: A list of weak references. Currently unused.
weakrefs = []

class PluginUnloadMonitor(object):
    """
    This class is used to monitor for a plugin being unloaded, which it does
    by executing code in its __del__ method. Currently it only prints a log message.
    """
    def __init__(self, name, when):
        self.name = name
        self.when = when
    def __del__(self):
        # early-binding log to a local so we don't reference globals in __del__
        try:
            asctime = time.asctime(time.localtime(self.when))
            PluginUnloadEvent("Plugin %s (loaded at %s) unloaded successfully" % (self.name, asctime)).dispatch()
        except:
            # If the interpreter is shutting down, then we may not be able to use logging
            pass

def _import_module(name):
    """
    Internal: Imports and returns a plugin module. Raises ImportError if not found.
    """
    f, filename, data = imp.find_module(name, ['plugins'])
    try:
        module = imp.load_module(name, f, filename, data)
    finally:
        f.close()
    return module


class Plugin(object):
    """
    Represents a plugin that is loaded into the PluginLoader.
    
    Note that the Plugin class is not the plugin module itself; that is accessed
    via the Plugin.module attribute.
    """

    def __init__(self, name):
        log.debug("Loading plugin %s..." % name)
        self.name = name
        self.module = self._load()
        self.disabled = False

        # Maintain a list of event subscriptions
        self.subscriptions = {}

        #: List of networks for which this plugin is enabled
        self.enabled_networks = []

        global plugins
        plugins[name] = self

        if hasattr(self.module, 'on_load') and callable(self.module.on_load): self.module.on_load()

    def _load(self):
        """Internal: Attempt to load (and return) the given module. Called on construction."""
        name = self.name
        try:
            module = _import_module(name)
            self.loadtime = (time.time(), time.asctime())
            # Inject a PluginUnloadMonitor into the module so we know when the GC unloads it
            module._monitor = PluginUnloadMonitor(name, self.loadtime[0])
            # Mark this module as a plugin
            module.__plugin__ = self
            return module
        except ImportError:
            # Do some smart analysis of the exception data
            errormsg = sys.exc_info()[1][0]
            frame = traceback.extract_tb(sys.exc_info()[2])[-1:][0]
            filename = frame[0]
            lineno = frame[1]
            if errormsg.startswith('cannot import name'):
                # The plugin module was successfully imported, but
                # it failed to import another module
                modname = frame[3].split()[1]
                errormsg = errormsg + " from module %s" % modname
            elif errormsg.startswith('No module named'):
                # The plugin module we are trying to load doesn't exist
                del frame # to avoid cyclic references
                log.warn('The plugin "%s" could not be found' % (name,) )
                return None
            del frame # to avoid cyclic references
            log.warn('Failed to load plugin "%s": %s line %d: %s' % (name, filename, lineno, errormsg) )
        except SyntaxError as e:
            log.warn('Plugin "%s" has invalid syntax' % (name,), exc_info=True)
        except Exception as e:
            log.warn('Failed to load plugin "%s": Unknown error: %s' % (name, e), exc_info=True)

        return None
        

    def unload(self):
        """
        Unloads this plugin and removes it from the PluginLoader. It will be disabled for all networks.
        """
        log.debug("Unloading plugin %s..." % self.name)
        self.unsubscribe()
        # Remove ourself from the PluginLoader's list
        global plugins
        del plugins[self.name]

        # Unload our module by removing all references
        del sys.modules[self.name]
        n = sys.getrefcount(self.module)
        if n > 2:
            log.debug("Warning: %d extra reference(s) to module %s still exist! This could be a memory leak!" % (n-2, self.name))
            import gc
            log.debug(gc.get_referrers(self.module))
        else:
            log.debug("Removing our final reference to %s" % self.name)
        self.module = None

    def reload(self):
        """
        Reloads this plugin's code from disk. This will disable and re-enable the plugin for all networks.
        """
        # TODO: Disable for all networks
        self.unsubscribe()

        # actual reload
        self.module = self._load()
        #reload(self.module)

        # TODO: Enable for all networks

    def disable(self, network):
        """
        Disables the plugin and runs its on_disable handler, if it has one.
        
        Any event handlers will no longer receive events from this network.
        """
        
        del network.enabled_plugins[self.name]
        if hasattr(self.module, 'on_disable') and callable(self.module.on_disable):
            # Call the method asynchronously
            Dispatcher.Async(self.module.on_disable)(network)

    def enable(self, network):
        """Enables the plugin and runs its on_enable handler, if it has one.

        Any event handlers will begin to receive events for this network."""

        def callback(result):
            network.enabled_plugins[self.name] = self

        if hasattr(self.module, 'on_enable') and callable(self.module.on_enable):
            # Call the method asynchronously
            Async(self.module.on_enable, callback=callback)(network)
        else:
            callback(None)
    
    def unsubscribe(self):
        """
        Unsubscribes from all subscribed events for this plugin.

        Optionally, only unsubscribe from events for a particular network. (how? why?)
        """
        subs = self.subscriptions.copy()
        # Work with a copy so we don't modify the list while iterating
        for func, event in subs.iteritems():
            event.unsubscribe(func)
        self.subscriptions = {}

@event_handler(PluginUnloadEvent)
def on_unload(event):
    log.info(event.plugin)

def get_plugin(name):
    """
    Attempts to retrieve and return a Plugin. If the plugin exists but is not yet loaded, it will be loaded and returned.
    """
    if name in plugins:
        return plugins[name]
    else:
        return Plugin(name)

log.debug("Plugin Loader initialized.")
