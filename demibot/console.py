# Python imports
import struct, sys, fcntl, termios
import signal, readline, threading, logging
import time

from ircstack._23support import unicode,str

# Set up logging
from ircstack.util import get_logger
log = get_logger(__name__)

# TODO: Use curses (or ncurses?)

active = False

stdoutlock = threading.RLock()

readline.parse_and_bind('set horizontal-scroll-mode on')

_unicode = True

def get_window_size():
    s = struct.pack('HHHH', 0, 0, 0, 0)
    t = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, s)
    return struct.unpack('HHHH', t)[:2]
cols, rows = get_window_size()

def setup():
    global cols, rows, active
    cols, rows = get_window_size()
    with stdoutlock:
        # [1;25r -Enable scroll area
        # [?6l  -???
        # [?7;25h -
        sys.stdout.write("\033[1;{0}r\033[?6l\033[?7;25h\033[{1};0f".format(cols-1, cols))
    active = True

def winch():
    with stdoutlock:
        sys.stdout.write("\033[1;{}r".format(cols-1))

def teardown():
    global active
    if active:
        active = False
        with stdoutlock:
            sys.stdout.write("\033[r\033[?6l\033[?7;25h\033[%d;0f\n\n" % cols)

def clearline(total=False):
    with stdoutlock:
        sys.stdout.write("\033[%dK" % (2 if total else 0))

def run():
    from demibot import IRCBot
    while(True):
        try:
            clearline()
            sys.stdout.write("\033[%d;0f" % cols)
            command = raw_input('> ')
            log.debug("User command: %s" % command)
            IRCBot().handle_console(command)
        except EOFError:
            IRCBot().quit_networks('Got Ctrl-D')
            break
        except KeyboardInterrupt:
            IRCBot().quit_networks('Got Ctrl-C')
            break
        except SystemExit as e:
            IRCBot().quit_networks(unicode(e))
            break
        except Exception:
            log.error("Exception occurred in console handler", exc_info=1)
    time.sleep(0.5)
    clearline(True)

class ConsoleHandler(logging.StreamHandler):

    def createLock(self):
        """
        Acquire a thread lock for serializing access to the underlying I/O.
        Overides logging.Handler.createLock().
        Sets the lock to our stdoutlock.
        """
        self.lock = stdoutlock

    def emit(self, record):
        """
        Emit a record.

        If a formatter is specified, it is used to format the record.
        The record is then written to the console. If
        exception information is present, it is formatted using
        traceback.print_exception and appended to the stream.  If the stream
        has an 'encoding' attribute, it is used to determine how to do the
        output to the stream.
        """
        try:
            msg = self.format(record)
            stream = self.stream
            fs = "\033[s\033[?25l\033[?6;7h\033[%d;0f\033E\n%%s\033[u\033[?25h" % (cols-2,) if active else "%s\n"
            if not _unicode: #if no unicode support...
                stream.write(fs % msg)
            else:
                try:
                    if (isinstance(msg, unicode) and
                        getattr(stream, 'encoding', None)):
                        ufs = u'\033[s\033[?25l\033[?6;7h\033[%d;0f\033E\n%%s\033[u\033[?25h' % (cols-2,) if active else u'%s\n'
                        try:
                            stream.write(ufs % msg)
                        except UnicodeEncodeError:
                            #Printing to terminals sometimes fails. For example,
                            #with an encoding of 'cp1251', the above write will
                            #work if written to a stream opened or wrapped by
                            #the codecs module, but fail when writing to a
                            #terminal even when the codepage is set to cp1251.
                            #An extra encoding step seems to be needed.
                            stream.write((ufs % msg).encode(stream.encoding))
                    else:
                        stream.write(fs % msg)
                except UnicodeError:
                    stream.write(fs % msg.encode("UTF-8"))
            self.flush()
        except (KeyboardInterrupt, SystemExit):
            raise
        except TypeError:
            return
        except:
            self.handleError(record)
