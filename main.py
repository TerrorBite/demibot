#!/usr/bin/env python
"""
This bootstrap script launches a demibot instance.
"""

import logging
import logging.config
import signal
import sys
import traceback
import threading
from os import path

def signal_handler(signum, frame):
    """
    Signal handler, handles SIGQUIT.

    This handler implements JVM-like behaviour - when Ctrl-\\ is pressed,
    it will dump a stacktrace for each running thread. Intended for debugging.
    """
    if signum == signal.SIGQUIT:
        output = []
        output.append("\n*** STACKTRACE - START ***\n")
        tnames = dict([(thread.ident, thread.name) for thread in threading.enumerate()])
        for threadId, stack in sys._current_frames().items():
            output.append("\n# Thread: %s" % tnames[threadId])
            for filename, lineno, name, line in traceback.extract_stack(stack):
                output.append('File: "%s", line %d, in %s' % (filename, lineno, name))
                if line:
                    output.append("  %s" % (line.strip()))
        output.append("\n*** STACKTRACE - END ***")

        logging.debug('\n'.join(output))
        try:
            with file('threaddump.txt', 'w') as f:
                print >> f, "\n*** STACKTRACE - START ***\n"
                for line in output:
                    print >> f, line
                print >> f, "\n*** STACKTRACE - END ***\n"
        except:
            logging.warn("Failed saving stacktrace to file")
        else:
            logging.info("Stacktrace written to threaddump.txt")

# Configure basic logging
logging.basicConfig(level=logging.DEBUG)
from demibot import IRCBot

def main():
    """
    Installs :py:func:`.signal_handler` to handle SIGQUIT, configures logging from
    logging.conf if it exists, then runs the :py:class:`demibot.IRCBot` singleton.
    """
    # Install signal handler
    signal.signal(signal.SIGQUIT, signal_handler)

    from ircstack.lib.stacklogger import StackFilter
    #logging.setLoggerClass(StackLogger)
    logging.getLogger().addFilter(StackFilter())

    if path.isfile('logging.conf'):
        logging.config.fileConfig('logging.conf')

    logging.info('Starting up...')

    # Start bot
    try:
        IRCBot().run()
    except KeyboardInterrupt:
        IRCBot().shutdown()
    except:
        logging.critical('An unhandled exception occurred in the main thread.', exc_info=True)
        # Attempt a clean shutdown (to end any non-daemon threads)
        IRCBot().shutdown()

if __name__ == '__main__':
    main()
