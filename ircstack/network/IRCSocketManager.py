"""
The IRCSocketManager is responsible for monitoring the sockets of IRCConnections
and notifying the relevant IRCConnection when data is available.
"""

# Python imports
from time import time
from threading import Thread, RLock
import socket
import logging
import select
import fcntl
import errno
import os

# Set up logging
from ircstack.util import get_logger, catch_all, hrepr, set_thread_name
log = get_logger()

class SockSignal:
    """
    Used to interrupt a select() call.

    Include SockSignal.signal in the list passed as the first parameter of select().

    Call SockSignal.set() to interrupt the select() call. SockSignal.signal will
    be in the list of readable fds returned by select().
    """
    log = get_logger()

    def __init__(self):
        self.signal, self._write = socket.socketpair()
        self.signal.setblocking(False)

        # Prevent blocking writes, otherwise set() tends to block if
        # reset() was never called.
        self._write.setblocking(False)

    def set(self):
        self.log.debug("Setting SockSignal")
        self._write.send('\0')
    def reset(self):
        try:
            self.signal.recv(1)
        except socket.error, e:
            if e.errno != errno.EAGAIN:
                self.log.exception("Socket error in SockSignal: This shouldn't happen!", e)
            pass

    def __del__(self):
        self._write.close()


thread = None

# Run as daemon (thread is killed when program exits)

running = False

# Connections with new sockets in a connecting state (i.e. wait-until-writable)
# Keys are sockets, values are time
connecting = {}

# Established connections which we should wait for data on (wait-until-readable)
# Only needed for select() backwards compatibility
#sockets = set()

# Maps file descriptors to socket objects, since epoll returns filenums
fdmap = {}

# This interrupts the select() call when shutting down, or when a socket dies,
# or when a new socket is registered
sig = SockSignal()

_lock = RLock()

def register(sock):
    """
    Takes a socket to manage. It expects a LineBufferedSocket that is in
    a connecting state.
    """
    if sock is None: return
    # Override the LineBufferedSocket's on_dead stub
    sock.on_dead = on_dead.__get__(sock, sock.__class__)

    if sock.error:
        # Connection failed on initial connect()
        # We shouldn't end up here though
        conn.on_connect_failed()
    else:
        with _lock:
            connecting[sock] = time()
            fdmap[sock.fileno()] = sock
            epoll.register(sock.fileno(), select.EPOLLOUT)
            #sig.set() # Shouldn't be needed
    log.debug("%s (fd %s) is registering" % (repr(sock), sock.fileno()))

def on_dead(sock, error=None):
    """
    Called by a LineBufferedSocket or derivative when it detects that
    its socket is dead (no longer connected).
    """
    with _lock:
        log.debug('Removing dead socket %s (fd %d) from IRCSocketManager' % (hrepr(sock), sock.fileno()))
        # Remove socket from fdmap if it is present
        # If not, abort
        if sock.fileno() in fdmap:
            del fdmap[sock.fileno()]
        else:
            log.debug("Warning: Socket %s (fd %d) isn't in our fdmap (was it registered?)"%(hrepr(sock), sock.fileno()))
            return
        try:
            # Try and unregister the socket
            epoll.unregister(sock.fileno())
        except IOError as e:
            if e[0] == 2:
                log.debug("Warning: Socket %s (fd %d) wasn't registered in epoll()"%(hrepr(sock), sock.fileno()))
            else:
                raise
    if sock.parent:
        # Check that this socket is actually the socket of its parent
        assert sock == sock.parent.socket

        if error:
            log.debug("Connection on socket %s lost: %s" % (hrepr(sock), repr(error)))
            log.info("Connection to %s lost: %s" % (sock.address, error[1]))
            sock.parent.on_error()
        else:
            log.info("Connection to %s closed." % (sock.address,))
            sock.parent.on_hangup()

def bad_fd(sock):
    """
    Takes a socket and checks if its file descriptor is valid.
    
    Currently unused.
    """
    try:
        fcntl.fcntl(sock.fileno(), fcntl.F_GETFL)
        return False
    except e as IOError:
        if e.errno == errno.EBADFD:
            return True

def _select_wait(timeout=10.0):
    """Performs a select() call and returns a tuple of sockets."""
    ready, connected, error = select.select(
            sockets + [sig.signal],
            connecting.keys(), 
            sockets + connecting.keys(),
            timeout) # select()
    results = []
    for sock in set(ready + connected + error):
        results.append((results.fileno(),
            (select.EPOLLIN if sock in ready else 0) &
            (select.EPOLLOUT if sock in connected else 0) &
            (select.EPOLLERR if sock in error else 0)
            ))

    return results

def _wait(timeout=10.0):
    return epoll.poll(timeout)

def start():
    """Starts the SocketManager thread."""
    global thread, running
    thread = Thread(name='SocketManager')
    thread.daemon = True
    thread.run = _run
    thread.start()
    running = True

@set_thread_name
@catch_all(retry=False)
def _run():
    """
    Main processing is done here.
    """

    log.info('IRCSocketManager is now running.')

    # Set up epoll object
    global epoll
    epoll = select.epoll()

    # Register socksignal into epoll socketset
    epoll.register(sig.signal, select.EPOLLIN)

    while running:
        # Reset signal
        sig.reset()

        # Wait for data, waking every 10 seconds to test for timed out sockets
        results = _wait(10.0)
        if not running: break

        for fd, event in results:
            with _lock:
                if fd == sig.signal.fileno():
                    # ignore SockSignal
                    continue
                socket = None
                if fd in fdmap:
                    socket = fdmap[fd]
                else:
                    # it's possible on_dead() was called while we are in this loop
                    # Ignore this socket if the fd is not in our map
                    continue

                if event & select.EPOLLERR:
                    # These sockets are in an error state
                    errn = socket.getsockopt(1, 4) # SOL_SOCKET, SO_ERROR
                    log.debug('%s had an error: [Error %d] %s' % (hrepr(socket), errn, os.strerror(errn)))
                    if socket in connecting.keys():
                        # Failure to connect, act accordingly
                        del connecting[socket]
                        socket.parent.on_connect_failed()
                    else:
                        # Existing connection failed, act accordingly
                        socket.parent.on_error()
                    del fdmap[fd]
                    epoll.unregister(fd)

                elif event & select.EPOLLIN:
                    # Socket has data waiting to be read
                    #log.debug('%s is ready to be read' % hrepr(socket))
                    conn = socket.parent.on_received()

                elif event & select.EPOLLOUT:
                    # Socket successfully connected (is ready to be written to)
                    # Set epoll to monitor it for reads instead
                    epoll.modify(fd, select.EPOLLIN)
                    del connecting[socket]
                    log.debug('%s has connected successfully' % hrepr(socket))

                    # Call connected handler on the connection
                    socket.parent.on_connected()

        # Check sockets that haven't connected yet for timeouts
        for socket, when in connecting.items():
            if when <= time()-30.0:
                log.debug("Detected connection timeout for %s" % repr(socket))
                # Connection timed out
                conn.on_connect_failed()

    sig.reset()
    epoll.close()
    # end while
    log.info('IRCSocketManager shut down cleanly.')

def stop():
    global running

    log.debug('Shutting down Socket Manager: Interrupting epoll')
    # Interrupt our select() call
    running=False
    sig.set()
    thread.join(0.2)


# If platform is not linux:

log.debug('Socket Manager initialized.')

