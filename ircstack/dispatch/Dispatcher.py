"""
The Dispatcher runs in its own thread and has two basic functions:

  * It accepts callables via dispatch() and calls them from its thread as soon as it can.
  * It accepts callables via schedule() and calls them after a specified delay.

More specifically, the Dispatcher is responsible for the following tasks in ircstack:

 1. It hands off incoming IRCMessages to an IRCNetwork for processing on its thread, to free up the network thread.
 2. It accepts Events generated by an IRCNetwork and distributes them to interested listeners (i.e. plugins).
 3. It executes scheduled tasks generated by the factories in the ircstack.dispatch.async module.
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

def schedule(task, delay):
    """
    Schedules a callable for execution after a given delay. Returns the callable.
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
    return task

def dispatch(event):
    """
    Adds a callable to the Dispatcher's queue to be called ASAP.
    
    Usually the callable is an Event which is ready to be processed and sent to listeners.

    Returns its parameter unmodified.
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

def shutdown():
    """
    Shuts down the Dispatcher.
    """
    global running
    # We stop the dispatcher by setting running to False and then waking the thread.
    if running:
        running = False
        inputq.put(None)
    # wait up to a second for the Dispatcher to terminate
    _thread.join(1.0)

def _process_tasks():
    """
    Internal: Check for and call any pending scheduled tasks.

    Returns time (in seconds) to wait on the input queue before stopping to process a scheduled task.
    """
    with _sched_lock:
        # Process all (over)due tasks (if any).
        while len(tasks) and tasks[0][0] < time():
            # Call the task
            # For tasks created via ircstack.dispatch.async, this will return almost immediately
            # as it just adds to the dispatcher's queue, or the threadpool's queue
            heappop(tasks)[1]()

        # Now return the time remaining until the next scheduled task (if any are left)
        if len(tasks):
            timeout = tasks[0][0]-time()
            # Return 0 for an overdue task (instead of a negative number),
            # This will cause the wait to return immediately
            return 0.0 if timeout<0 else timeout
        else:
            # Return None (which means "forever") if there are no tasks
            return None

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
        # Process pending tasks and receive timeout for our Queue.get() call.
        # If there are no scheduled tasks in future, it returns None which will never timeout.
        timeout = _process_tasks()

        try:
            # Block until work arrives or timeout, whichever comes first
            work = inputq.get(True, timeout)
        except QueueEmpty, e:
            # Timeout means that we have a scheduled task due to be completed.
            # Jump back to the top of the loop where we call _process_tasks()
            continue
        
        if work is None:
            # None was pushed to the queue to wake our thread. This means that either:
            # * a new task was added, thus requiring the timeout to be recalculated, or
            # * the Dispatcher is shutting down, and we need to exit the loop.
            continue

        # If we did get a callable from the queue (probably an Event or Task), call it
        if callable(work):
            try:
                work()
            except Exception, e:
                log.exception("Exception occurred while dispatching event")
        else: log.debug("Dispatcher got non-callable %s" % event)

    # end while
    # We exited the loop, so begin shutdown procedure
    if _threadpool:
        _threadpool.stop()
        _threadpool = None

    log.info('Dispatcher shut down cleanly.')

log.debug("Event Dispatcher and Scheduler initialized.")
