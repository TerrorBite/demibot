import os
import socket
import ssl
import errno
import re
import weakref
import threading
from time import time

# Set up logging
from ircstack.util import get_logger
log = get_logger()

from ircstack.util import PriorityQueue, catch_all, humanid, hrepr
from ircstack.dispatch.async import DelayedTask

class SocketClosedError(socket.error):
    """
    Raised when an operation is performed on a closed socket.
    """
    def __init__(self):
        super(SocketClosedError, self).__init__(errno.EBADF, 'Bad file descriptor')

# ircstack.network.sockets
class LineBufferedSocket(socket.socket):
    """
    An extension of a socket, which buffers input until a complete line is received.
    This implementation is also SSL-capable (using the ssl module).

    Keeps a record of the last line fragment received by the socket, which is usually not a complete line.
    It is prepended onto the next block of data to make a complete line.
    """

    # Inheritance notes: Under normal circumstances, we will actually be inheriting
    # from socket._socketobject. In practice this doesn't matter.

    log = get_logger()
    netlog = get_logger('network')
    def __init__(self, family=socket.AF_INET, parent=None, use_ssl=False):
        """
        Constructs a new LineBufferedSocket that will connect to the specified address.
        """
        self._recvbuf = ''
        self._sendbuf = ''
        self._lines = []
        self._suspended = False

        self.address = None
        self.error = None

        # In order to be helpful to higher-level code, store a reference to
        # some optional "parent" object. We don't use it or care what it is.
        self.parent = parent

        # Use a pair of re-entrant locks to prevent threading issues.
        self._readlock = threading.RLock()
        self._writelock = threading.RLock()
        
        super(LineBufferedSocket, self).__init__(family=family, type=socket.SOCK_STREAM, 
                # If using SSL, wrap another socket in an SSL layer and tell base class to use the
                # wrapped socket internally. The SSL layer should be almost completely transparent to us.
                _sock=ssl.wrap_socket(socket.socket(family=family, type=socket.SOCK_STREAM),
                    ssl_version=ssl.PROTOCOL_TLSv1) if use_ssl else None
                )

        # After this point the following methods will directly reference
        # the methods of the underlying socket implementation:
        #     recv,  recvfrom,  recv_into,  recvfrom_into,   send,  sendto

        # If we wish to wrap any of these we must now redefine them.
        # Override send() with our _send() implementation.
        # To actually send data we'll use self._sock.send() where _sock
        # is the native socket implementation.
        setattr(self, 'send', self._send)

        # Enable TCP keepalives and make self non-blocking
        self.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, True)
        self.setblocking(False)

    #def close(self):
    #    super(LineBufferedSocket, self).close() 

    def connect(self, address):
        with self._readlock, self._writelock:
            self.address = address
            self.log.info('Opening connection to %s:%d' % address)
            # Call base class connect
            result = super(LineBufferedSocket, self).connect_ex(address)
            if result==errno.EINPROGRESS:
                # This is normal (because we're nonblocking)
                return True
            elif result!=0:
                self.error = socket.error(result, os.strerror(result))
                self.log.error('Error trying to connect: %s' % str(self.error))
                # Take appropriate action?
                return False
            else:
                # we might end up here if the socket connected fast enough
                self.log.debug("Connected immediately %s" % self)
                return True

    def connect_ex(self, address):
        """See socket.connect_ex"""
        with self._readlock, self._writelock:
            self.address = address
            return super(LineBufferedSocket, self).connect_ex(address)
    
    def __repr__(self):
        return hrepr(self)

    def disconnect(self):
        """
        A higher-level method for disconnecting the socket cleanly.
        This does not close the socket; you still need to manually call close().
        """
        with self._readlock, self._writelock:
            try:
                self.shutdown(2) # SHUT_RDWR, using constant in case of calling from destructor
                self._cleanup()
            except socket.error as msg:
                if msg[0]==errno.ENOTCONN:
                    self.log.debug("%s was already disconnected in disconnect()" % repr(self))

    def _recv(self):
        """
        Internal function: Receives new data from the socket and splits it into lines.
        Last (incomplete) line is kept for buffer purposes.
        Returns nothing; received data is stored internally. Call readlines() to retrieve it.
        """
        with self._readlock:
            data = ''
            try:
                data = self._recvbuf + self._sock.recv(4096)
                self.netlog.info('%s >>> %s' % (self.address, repr(data)))
            except ssl.SSLError as e:
                if e[0] == ssl.SSL_ERROR_WANT_READ:
                    # The SSL layer needs us to call read() again before it can return
                    # actual data to us, because it needs to do SSL stuff (i.e. handshaking)
                    # behind the scenes.
                    # We simply return, and the SocketManager will call us again.
                    return
                elif e[0] == ssl.SSL_ERROR_WANT_WRITE:
                    # The SSL layer needs us to perform a write operation so that it can do SSL
                    # stuff before it is able to return data to us.
                    # We attempt to send an empty string (zero bytes).
                    self._send('')
                    return
            except socket.error as msg:
                if msg[0] in [errno.EAGAIN, errno.EWOULDBLOCK]:
                    # No data to read, nothing to do here.
                    return
                elif msg[0]==errno.ENOTCONN:
                    # Transport endpoint is not connected. This shouldn't happen!
                    self.log.debug('%s is not connected!' % repr(self))
                    return
                elif msg[0] in (errno.ECONNRESET, errno.ETIMEDOUT, errno.EPIPE):
                    # A network error occurred.
                    self._cleanup(msg)
                    return
                # Unknown error, re-raise
                raise
            except socket.timeout:
                # Only raised when a blocking socket read times out.
                # Our socket is nonblocking so we should never end up here
                return

            if data == '':
                # Empty data on read means the other side has hung up on us.
                self.log.info('Remote host has disconnected cleanly')
                self._cleanup()

            self._lines += data.split("\r\n")
            self._recvbuf = self._lines[-1]
            self._lines = self._lines[:-1]

    def _cleanup(self, error=None):
        """Internal: Called immediately when the connection dies"""
        with self._readlock, self._writelock:
            self.error = error
            # Call on_dead handler
            self.on_dead()
    
    def on_dead(self):
        """
        Override this method to run code when the socket dies, but before cleanup.
        The default implementation in LineBuffereredSocket is a no-op.
        """
        pass

    def is_ready(self):
        """
        Reads the socket and returns True if there are complete lines waiting to be retrieved.
        """
        with self._readlock:
            self._recv()
            return len(self._lines) > 0

    def readlines(self):
        """
        Reads from the socket and returns all complete lines that were retrieved.
        Can return an empty list if there has been no complete line received.
        """
        with self._readlock:
            self._recv()
            ret = self._lines
            self._lines = []
            return ret

    def readline(self):
        """
        Returns the next available line from the network, or None if no complete line has been received.
        It is recommended that you call readlines() instead of this method.
        """
        with self._readlock:
            if len(self._lines) == 0:
                self._recv()
            return self._lines.pop(0) if len(self._lines) else None

    def _send(self, data):
        # Acquire write lock
        with self._writelock:
            if self._suspended:
                self.log.debug("Attempt to send data while suspended, buffering: %s" % repr(data))
                self._sendbuf += data
                return
            #log.debug(data)
            self._sendbuf, data = '', self._sendbuf+str(data)
            if not data: return True
            #self.log.debug("_send() %s %s" % (repr(self), repr(data)))
            try:
                l = self._sock.send(data)
                # log it
                self.netlog.info('%s <<< %s' % (self.address, repr(data[:l])))
                if l < len(data):
                    self.log.warn("Not all data could be sent, trying again")
                    self._sendbuf = data[l:]
                    DelayedTask(self._send, 0.1)('')
            except socket.error as e:
                if e[0]==errno.EAGAIN:
                    self.log.warn("Error when sending some data, trying again")
                    self._sendbuf = data
                    DelayedTask(self._send, 0.1)('')
                elif e[0] in (errno.ECONNRESET, errno.ETIMEDOUT, errno.EPIPE):
                    # A network error occurred.
                    self._cleanup(e)
                return False
            return True
    
    def suspend(self, data=None):
        """
        (Method used for STARTTLS)
        Suspends writes, after first sending the provided data (if any) in an
        atomic operation, guaranteeing that data is the final thing sent.
        Further attempts to write will be buffered until resume() is called.
        """
        with self._writelock:
            if data: self._send(data)
            self._suspended = True

    def resume(self):
        """
        (Method used for STARTTLS)
        Resumes writes after a call to suspend(), and also immediately sends
        any data that was buffered while suspended.
        """
        self._suspended = False
        self._send('')

    def starttls(self):
        """
        Initiates a secure connection over the existing socket.
        If the socket is already using SSL, does nothing (no onion layering!)
        """
        # Should we raise a RuntimeError or just silently ignore?
        if isinstance(self._sock, ssl.SSLSocket): return

        # lock while doing this
        with self._readlock, self._writelock:

            # Wrap our existing socket in SSL
            self.setblocking(True)
            self._sock=ssl.wrap_socket(self.dup())
            self.setblocking(False)

            # Reassign methods (except send())
            for method in ssl._delegate_methods:
                if method != 'send': # already ours
                    setattr(self, method, getattr(self._sock, method))
            self.resume()


# ircstack.network.sockets
class SendThrottledSocket(LineBufferedSocket):
    """
    An extension of LineBufferedSocket that throttles outgoing messages.

    Detects flooding and queues outbound messages to avoid flooding the IRC server.
    """
    log = get_logger()

    def __init__(self, family=socket.AF_INET, parent=None, use_ssl=False, rate=10.0, per=6.0):

        # These two parameters are important (and for now, are hardcoded).
        # At its most basic: Allows "rate" messages every "per" seconds.

        # rate: Controls how many messages may be sent before flood protection kicks in.
        #       This value determines the max "allowance" for sending.
        self.rate = rate

        # per:  Controls how many seconds it takes for the "allowance" to reach full value
        #       again, thus how long until another "rate" messages may be sent unhindered.
        self.per = per

        self.allowance = self.rate
        self.last_check = time()

        self.sendq = PriorityQueue()
        super(SendThrottledSocket, self).__init__(family, parent, use_ssl)

    def _update_allowance(self):
        """
        Update (increase) the send queue allowance depending on time.

        This system works by giving the send queue an "allowance" which increases
        over time (capped at a certain value). Value is removed from this
        "allowance" to "pay" for sending a line. If there is not enough to "pay"
        for the line, it is queued until sufficient "funds" accumulate to send it.
        """
        current = time()
        time_passed = current - self.last_check
        self.last_check = current

        # Work out how much the allowance has increased since the last time this was called.
        self.allowance += time_passed * (self.rate / self.per)
        if self.allowance > self.rate: self.allowance = self.rate

    def send_now(self, message):
        """
        Sends a message, bypassing the send queue, however this still consumes
        send queue allowance.

        Use this only for replying to a PING, or for sending a PING when we
        are measuring lag (and don't want it delayed). Use sparingly.
        """
        if super(SendThrottledSocket, self).send(message+'\r\n'):
            self.allowance -= 1.0

    def _get_delay(self):
        """
        Returns the minimum time until a single line may next be sent to the server.

        This works by calculating how long it will take for the "allowance" to exceed the
        "cost" of sending a line, i.e. 1. It returns zero if there is no delay and a line
        would be sent instantly.
        """

        # Time until allowance recharges enough to send a single message
        delay = self.per * (1-self.allowance) / self.rate
        return delay if delay>0 else 0
        
    def _get_queue_delay(self):
        """Returns the minimum time until all messages currently in the send queue would be sent."""
        # Plus the minimum time needed to send the rest of the queue
        return self._get_delay() + len(self.sendq) * self.per / self.rate 

    def sendline(self, message, priority=1):
        """
        Sends a message to the socket, delaying it if neccessary.
        Takes an optional priority value, which defaults to 1.
        """

        # TODO: remove debug message
        self.log.debug(repr(message))

        self._update_allowance()
        if self.allowance < 1.0 or self.sendq.has_items():
            # Exceeded rate limit, queue the message for later. OR
            # Messages are currently queued, queue this one too so we don't send out of order.

            delay = self._get_queue_delay() # Estimate required delay before adding message to queue.
            # Add message to queue.
            self.sendq.put(message+'\r\n', priority)
            # Request a callback to process the queued message
            DelayedTask(self.tick, delay)()
            self.log.debug("Throttling engaged: len(sendq)=%d, allowance=%.2f, delay=%.2f" % (len(self.sendq), self.allowance, delay))
        else:
            # No queue. Send the data immediately.
            if super(SendThrottledSocket, self).send(message+'\r\n'):
                self.allowance -= 1.0

    def tick(self):
        """
        Callback to process the send queue.

        It doesn't hurt to call this manually if for some reason you need to
        check if outgoing queued messages are ready to be sent.
        """
        # Ignore tick if this socket is dead! Don't schedule another
        if isinstance(self._sock, socket._closedsocket): return
        
        # Update send allowance
        self._update_allowance()
        self.log.debug("Got tick: len(sendq)=%d, allowance=%.2f" % (len(self.sendq), self.allowance))

        # Pop and send only if there is data to send and allowance to send it with
        if self.sendq.has_items():
            if self.allowance >= 1.0:
                self.send_now(self.sendq.get())
            else:
                delay = self._get_delay()
                self.log.debug('Ticked, but not enough allowance! allowance=%f len(sendq)=%d\n'
                    'Rescheduling tick in %.2f seconds.' % (self.allowance, len(self.sendq), delay))
                DelayedTask(self.tick, delay)()
        else:
            self.log.debug('Ticked, but the queue is empty! Nothing to send!')
    
# ircstack.network.sockets
class ThrottleWorker(threading.Thread):
    """
    This is the thread responsible for calling tick() on SendThrottledSockets
    that request it, so that their send queues can be processed in a timely manner.
    
    DEPRECATED: The ThrottleWorker thread has been deprecated. SendThrottledSockets
    now utilise ircstack.dispatch.async to process their send queues.
    """
    # Change to use a heap or something similar instead of a dict?
    # NOTE: Deprecated in favor of using the Dispatcher

    def __init__(self):
        log.warning("DEPRECATED: ThrottleWorker is deprecated! This shouldn't be used!")
        threading.Thread.__init__(self, name='ThrottleWorker')
        self.e = threading.Event()
        self.running = True
        self.items = {}
        log.info('ThrottleWorker initialized.')

    def set_timeout(self, buf, timeout):
        """
        Calls back the given buffer after a certain period of time has passed.

        buf: The buffer to call back.
        timeout: A floating-point value in seconds of how long to wait.
        """
        # Add this buffer to the list of items.
        self.items[time() + timeout] = weakref.ref(buf, _remove)

        # Poke the main loop (so we can potentially wait for this timeout)
        self.e.set()

    def _remove(self, buf):
        """
        Called when a buffer which we hold a weak reference to is garbage collected.
        This allows us to remove it from our collection.
        """
        for key in [k for k, ref in self.items.iteritems() if ref is buf]:
            del self.items[key]
    
    @catch_all(retry=False)
    def run(self):
        "Runs this ThrottleWorker thread."
        log.info('ThrottleWorker now running.')
        while self.running:
            # Get the earliest event in the list (if any)
            nextevent = min(self.items.keys()) if self.items else None

            # Check if the earliest event (if any) is due now or in the past.
            while nextevent and nextevent <= time():

                # This event is due! Let's go!~
                self.items.pop(nextevent)().tick()

                # Get next event after this one, if it exists
                nextevent = min(self.items.keys()) if self.items else None

            # Now all due events have been processed. Check if a future event exists.
            if nextevent is None:
                # No more events! Wait indefinitely for one to be added.
                self.e.wait()
                continue
            else:
                # An event is due in the future, wait for it to occur.
                self.e.wait(nextevent - time() + 0.001)
                continue
        # End while self.running. Exit gracefully.
        log.info('ThrottleWorker shut down cleanly.')

    def stop(self):
        "Shuts down the ThrottleWorker. Remaining events are not processed."
        # Set running to false then trigger the loop to run.
        self.running = False
        self.e.set()

