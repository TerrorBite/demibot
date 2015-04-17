"""
The Dispatcher is responsible for two tasks:

1. Accepting incoming IRC messages from an IRCConnection and handing it off to an IRCNetwork for processing
2. Accepting Events from an IRCNetwork and distributing them to subscribed plugins for processing
"""

# Compatibility
from __future__ import with_statement # for Python 2.5

# Python imports
import threading
from Queue import Queue, Empty as QueueEmpty
from time import time
from heapq import *

# Set up logging
from ircstack.util import get_logger, catch_all, set_thread_name
log = get_logger(__name__)

__all__ = ['process', 'run', 'stop', 'join', 'dispatch']

# NOTE: The Dispatcher is imported by most ircstack modules.
# As an early import, it should avoid importing any other ircstack code.

# Initialize the Dispatcher
running = False

# The input queue holds data submitted by an IRCConnection.
inputq = Queue()

# The output queue is managed via the DispatchThreadPool.
_threadpool = None

# This locks the scheduler to prevent concurrent modifications.
_sched_lock = threading.RLock()

# This is our task queue heap
tasks = []

def _schedule(task, delay):
    """
    Internal: Used by DelayedTask and RepeatingTask to schedule a new task for future execution.
    """
    with _sched_lock:
        # Work out if timeout value will need updating
        #need_update = len(tasks) and tasks[0][0] <= delay

        # Push scheduled task onto the stack
        heappush(tasks, (time()+delay, task))
        
        # Poke the Dispatcher's input queue to wake up, only if the wait time needs updating
        #if need_update:
        #    log.debug("_schedule() bumping Dispatcher for timeout update")
        #    inputq.put(None)
        inputq.put(None)

def dispatch(event):
    """
    Adds an Event to the Dispatcher's queue in order to be processed and sent to listeners.
    """
    # Add the IRCEvent to the queue.
    inputq.put(event)
    # Allow method chaining
    return event

def start():
    """
    Starts the Dispatcher running in its own thread.
    """
    global _thread
    if not running:
        _thread = threading.Thread(name='Dispatcher', target=_run)
        _thread.start()

def stop():
    """
    Shuts down the Dispatcher.
    """
    global running, _threadpool
    # Stop the dispatcher by setting running to False and then waking the thread.
    if running:
        running = False
        if _threadpool:
            _threadpool.stop()
            _threadpool = None
        inputq.put(None)
        # TODO: Other stuff

def join(timeout):
    _thread.join(timeout)

def _get_timeout():
    """
    Internal: Calculates how long to wait on the input queue before stopping to process a scheduled task.
    """
    with _sched_lock:
        if len(tasks):
            timeout = tasks[0][0]-time()
            # Return 0 for an overdue task (instead of a negative number)
            return 0.0 if timeout<0 else timeout
        else:
            return None

def _process_tasks():
    """
    Internal: Processes any pending scheduled tasks.
    """
    with _sched_lock:
        # First, check if any tasks exist
        if len(tasks):
            # Process all (over)due tasks. This should normally only be one but we loop just in case
            while len(tasks) and tasks[0][0] < time():
                # Call the DelayedTask (or subclass) _exec method
                heappop(tasks)[1]._exec()

@set_thread_name
@catch_all(retry=False)
def _run():
    """
    Internal: Dispatcher main loop. Waits for new items in the input queue,
    and for scheduled tasks to become due.
    """
    global running
    running = True
    log.info('Dispatcher started')

    from ircstack.dispatch.async import DispatchThreadPool
    global _threadpool
    _threadpool = DispatchThreadPool(4)
    
    while running:
        # Calculate timeout for next Queue.get()
        # If there are no scheduled tasks in future, it returns None which blocks indefinitely.
        timeout = _get_timeout()

        try:
            # Block until new event or timeout, whichever comes first
            event = inputq.get(True, timeout)
        except QueueEmpty, e:
            # Timed out waiting for data.
            # Timeout means that we have a scheduled task due to be completed.
            _process_tasks()
            continue
        
        if event is None:
            # None was pushed to the queue. This is a no-op to wake the thread. It occurs:
            # * When a new task is added, thus requiring the timeout to be recalculated.
            # * If the Dispatcher is shutting down, and the main loop needs to clean up and exit.
            continue

        # Tell the event to call its subscribers
        if callable(event):
            try:
                event()
            except Exception, e:
                log.exception("Exception occurred while dispatching event")
        else: log.debug("Dispatcher got non-callable %s" % event)

    # end while
    log.info('Dispatcher shut down cleanly.')

log.debug("Event Dispatcher and Scheduler initialized.")
