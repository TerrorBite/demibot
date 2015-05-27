import sys
import threading
from Queue import Queue

from . import Dispatcher

# Set up logging
from ircstack.util import get_logger, catch_all, set_thread_name
log = get_logger()

__all__ = ['Sync', 'Async', 'SyncDelayed', 'AsyncDelayed', 'SyncRepeating', 'AsyncRepeating',
    'ThreadPool', 'DispatchThreadPool', 'ThreadPoolWorker']

class ThreadPool(object):
    "Generic threadpool implementation."
    # We don't want to rely on the presence of concurrent.futures
    # so we implement our own ThreadPool (not difficult to do)
    log = get_logger()

    def __init__(self, threads=4, basename="PoolWorker"):
        self.workers = []
        self.queue = Queue()

        # Initialize worker threads
        for tid in range(threads):
            worker = ThreadPoolWorker(self.queue, "%s%d" % (basename, tid))
            worker.start()
            self.workers.append(worker)

    def stop(self):
        """
        Shuts down the threadpool by killing all workers.
        """
        for tid in range(len(self.workers)):
            # Kill workers by making them raise SystemExit via sys.exit()
            # We simply run as many sys.exit tasks as there are workers
            self.invoke(sys.exit)

    def invoke(self, target):
        """
        Invoke a target callable with no arguments or callback.
        
        To provide arguments and/or request a callback, use get_proxy to wrap your target
        in a proxy method, then call the proxy. See documentation for get_proxy.
        """
        if callable(target):
            # add a task to the queue with no args or kwargs
            self.queue.put((target, (), {}, None))
        else:
            raise ValueError("First argument must be callable")

    def Proxy(self, target, callback=None):
        """
        Factory function, returns a proxy callable for a target callable.

        When the proxy is called, it will return immediately. The target function
        will then be called in turn by a worker thread.

        This can be done in one line, for example: Proxy(target)(arg1, arg2)
        You can keep the proxy object and call it multiple times.
        
        The callback function, if provided, must take a single argument and will
        be called (on the worker thread) with the return value of the target passed
        as its sole argument.
        """
        def proxy(*args, **kwargs):
            self.queue.put((target, args, kwargs, callback))
        return proxy

class ThreadPoolWorker(threading.Thread):
    """
    Simple ThreadPool worker thread.

    It waits on a Queue maintained by the ThreadPool for work. Work is a tuple
    consisting of (target, args, kwargs, callback) where target is a callable,
    args and kwargs are the arguments to call target with, and callback is an
    optional (may be None) callback method.
    """
    log = get_logger()

    def __init__(self, queue, name):
        super(ThreadPoolWorker, self).__init__(name=name)
        self.daemon = True # die upon interpreter exit
        self.queue = queue

    @set_thread_name
    @catch_all(retry=True)
    def run(self):
        self.log.debug('%s now waiting for work' % self.name)
        while True:
            # Block on queue.get() until work arrives, then unpack work
            target, args, kwargs, callback = self.queue.get()

            # Call target
            results = target(*args, **kwargs)
            
            # Run callback if it is callable
            if callable(callback):
                callback(results)

class DispatchThreadPool(ThreadPool):
    
    def __init__(self, threads):
        super(DispatchThreadPool, self).__init__(threads, basename="DispatchWorker")

    def invoke_handler(self, func, event):
        "Executes an event handler on the thread pool."
        self.Proxy(func)(event)

def Async(target, callback=None, fail_if_sync=False):
    """
    Factory function that returns a callable proxy object that can be used to
    execute a callable asynchronously on the Dispatcher's internal threadpool.

    If the threadpool is not running, this will silently fail by returning a
    proxy that calls the target synchronously. If you need to guarantee that
    your target is called asynchronously, pass in fail_if_sync=True and a
    RuntimeError will be raised instead if the threadpool is not running.
    """
    if Dispatcher._threadpool:
        # Threadpool is running, return a threadpool proxy
        return Dispatcher._threadpool.Proxy(target, callback)
    elif fail_if_sync:
        # Threadpool is not running and caller requested failure
        raise RuntimeError("Cannot return an async proxy for target because the DispatchThreadPool is not running.")
    elif callback and callable(callback):
        # Threadpool not running, callback provided - fail silently by returning
        # a proxy that calls target synchronously and passes result to callback synchronously
        def proxy(*args, **kwargs):
            callback(target(*args, **kwargs))
        return proxy
    else:
        # Threadpool not running, no callback provided - fail silently
        # Nothing to do here - return the actual target in lieu of a proxy
        return target

def Sync(target, callback=None):
    """
    Factory function that returns a callable proxy object that can be used to
    execute a callable on the Dispatcher's main thread.
    
    Use this to call a function synchronously from a threadpool worker, or to
    schedule code to be run after an event handler completes.
    """
    def proxy(*args, **kwargs):
        def task(): # This function will be called directly by the Dispatcher.
            # Call target
            results = target(*args, **kwargs)
            
            # Run callback if it is callable
            if callable(callback):
                callback(results)
        # Now drop the task we just created into the Dispatcher's queue
        Dispatcher.dispatch(task)
    return proxy
    pass

def _delay(target, delay, callback=None, repeat=False, factory=Sync):
    # Obtain proxy object that, when called, will execute the target on some thread
    proxy = factory(target, callback)

    # Create ScheduledTask class
    class ScheduledTask(object):
        """
        Represents a running scheduled task that has been started and is waiting for execution.

        Calling an instance of this class will execute the task.
        """
        #log = get_logger()
        def __init__(self, *args, **kwargs):
            self.running = True

            # Store arguments for target
            self._args, self._kwargs = args, kwargs

        def __call__(self):
            # if we are not running, do nothing
            if self.running:
                # Reschedule if we are a repeating task, otherwise we are no longer running
                if repeat: Dispatcher.schedule(self, delay)
                else: self.running = False

                # Call proxy to execute target on the Dispatcher or its threadpool
                proxy(*self._args, **self._kwargs)
                
        def abort(self):
            """
            Cancels this task. The task will not execute after this method is called.

            Calling this method after the task has executed will have no effect.
            """
            self.running = False

    # Return wrapper that schedules the task
    return lambda *args, **kwargs: Dispatcher.schedule(ScheduledTask(*args, **kwargs), delay)

def SyncDelayed(target, delay, callback=None):
    """
    This factory function returns a wrapper around the target. When this wrapper is called (with optional arguments),
    it schedules a task to be run after the given delay and immediately returns the task object. When executed by
    the Dispatcher, the scheduled task will call target on the Dispatcher's thread after at least the given time
    period has elapsed, providing the target with the same arguments that were provided to the wrapper.
    
    While this sounds quite complicated, it's actually easy to use, as the following examples demonstrate.

    Here, we are calling a hypothetical print_message function, as follows:

        print_message("I like trains")

    Let's say we want to delay printing of the message by five seconds. We use SyncDelayed to create the wrapper:

        delayed_print_message = SyncDelayed(print_message, 5.0)
        task = delayed_print_message("I like trains")

    Or, if we just want to make a single call in a delayed manner, we can use this condensed syntax:

        task = SyncDelayed(print_message, 5.0)("I like trains")

    Note that the wrapper returns the task object that it scheduled with the Dispatcher. This object has an
    abort() method that can be called to abort the task if we decide we no longer want it to run. Of course,
    if the task has already been executed, calling abort() will have no effect.

    We can call delayed_print_message exactly as though it was the original print_message.  The wrapper can be
    stored, called multiple times, and will schedule a new task each time it is run. This makes it possible to
    pass the wrapper to any code which was expecting the original function, as long as that code doesn't
    consider the return value of the function (see below).

    If the Dispatcher is busy, then the task may be delayed for longer than requested; however, a task will never
    be executed prematurely.
    
    If you require access to the return value of the target, you can register a callback that will be
    called with the return value after the target completes. Just pass the callback after the delay parameter:

        def callback(retval):
            # do something with the return value here
            print repr(retval)
        SyncDelayed(target, delay, callback)("arguments to target")

    See also: AsyncDelayed(), SyncRepeating(), AsyncRepeating()
    """
    return _delay(target, delay, callback, repeat=False, factory=Sync)

def AsyncDelayed(target, delay, callback=None):
    """
    Identical to SyncDelayed(), except that the target will be executed on a threadpool worker instead of on
    the main Dispatcher thread.

    Note that the Dispatcher still needs to launch the async task, so if the Dispatcher is busy, the task
    will still be delayed for longer than requested.
    """
    return _delay(target, delay, callback, repeat=False, factory=Async)

def SyncRepeating(target, delay, callback=None):
    """
    Identical to SyncDelayed(), except that instead of executing only once, the task will continue to be
    executed periodically on the main Dispatcher thread.

    Note: It is essential to store the returned task object, since calling abort() is the only way to
    stop a repeating task from running.
    """
    return _delay(target, delay, callback, repeat=True, factory=Sync)

def AsyncRepeating(target, delay, callback=None):
    """
    Identical to SyncRepeating(), except that the target will be executed on a threadpool worker instead of on
    the main Dispatcher thread.
    """
    return _delay(target, delay, callback, repeat=True, factory=Async)

