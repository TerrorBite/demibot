#!/usr/bin/env python
import logging
import logging.config
import signal
import sys
import traceback
import threading

def h_SIGQUIT(signum, frame):
    code = []
    code.append("\n*** STACKTRACE - START ***\n")
    tnames = dict([(thread.ident, thread.name) for thread in threading.enumerate()])
    for threadId, stack in sys._current_frames().items():
        code.append("\n# Thread: %s" % tnames[threadId])
        for filename, lineno, name, line in traceback.extract_stack(stack):
            code.append('File: "%s", line %d, in %s' % (filename, lineno, name))
            if line:
                code.append("  %s" % (line.strip()))
    code.append("\n*** STACKTRACE - END ***")

    logging.debug('\n'.join(code))
    try:
        with file('threaddump.txt', 'w') as f:
            print >> f, "\n*** STACKTRACE - START ***\n"
            for line in code:
                print >> f, line
            print >> f, "\n*** STACKTRACE - END ***\n"
    except:
        logging.warn("Failed saving stacktrace to file")
    else:
        logging.info("Stacktrace written to threaddump.txt")
signal.signal(signal.SIGQUIT, h_SIGQUIT)

logging.basicConfig(level=logging.DEBUG)
from ircstack.lib.stacklogger import StackFilter
#logging.setLoggerClass(StackLogger)
logging.getLogger().addFilter(StackFilter())

# Configure logging
logging.config.fileConfig('logging.conf')

from demibot import IRCBot

def main():

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
