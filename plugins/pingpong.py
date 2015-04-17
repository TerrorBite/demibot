
# If you say "!ping", the bot will reply "Pong!"

# This serves as a sample plugin.

from ircstack.dispatch.events import CommandEvent, EndOfMOTDEvent, event_handler

def on_load():
    """Called when this plugin is loaded, enabling it to perform tasks such as reading configuration."""
    print "Plugin pingpong is loaded!"

def on_enable(network):
    """Called when this plugin is enabled for a particular network."""
    print "Plugin pingpong is enabled for %s!" % network

def on_disable(network):
    print "Plugin pingpong is disabled for %s!" % network

@event_handler(CommandEvent, command='ping')
def on_ping_command(event):
    event.reply('Pong!')

@event_handler(CommandEvent)
def on_any_command(event):
    event.reply('You used the "%s" command with arguments: %s.' % (event.command, event.message))
