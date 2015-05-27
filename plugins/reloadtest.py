
# If you say "!ping", the bot will reply "Pong!"

# This serves as a sample plugin.

from ircstack.dispatch.events import CommandEvent, event_handler

@event_handler(CommandEvent, command='test')
def on_ping_command(event):
    event.reply('Message two')
