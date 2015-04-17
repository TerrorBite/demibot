import sys
import threading
from Queue import Queue

from . import Dispatcher

# Set up logging
from ircstack.util import get_logger, catch_all, set_thread_name
log = get_logger()

__all__ = ['DelayedTask', 'DispatchThreadPool', 'RepeatingTask', 'ThreadPool',
    'ThreadPoolWorker', 'Async', 'Sync']

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
        for tid in range(len(self.workers)):
            # Kill workers by asking them to run sys.exit()
            self.invoke(sys.exit)

    def invoke(self, target):
        """
        Invoke a target callable with no arguments or callback.
        
        To provide arguments and/or a callback, use get_proxy to wrap your target
        in a proxy method, then call the proxy. See documentation for get_proxy.
        """
        if callable(target):
            self.queue.put((target, (), {}, None))
        else:
            raise ValueError("First argument must be callable")

    def Proxy(self, target, callback=None):
        """
        Returns a proxy object for a target callable.

        When the proxy is called, it will return immediately. The target function
        will be called in turn by a worker thread.

        This can be done in one line, for example: Proxy(target)(arg1, arg2)
        You can keep the proxy object and call it multiple times.
        
        The callback function, if provided, must take a single argument and will
        be called with the return value of the target as sole argument.
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
        # Threadpool not running, callback provided - fail silently
        # Return proxy that calls target synchronously and passes result to callback synchronously
        def proxy(*args, **kwargs):
            callback(target(*args, **kwargs))
        return proxy
    else:
        # Threadpool not running, no callback - fail silently
        # Just return the target itself, they'll never know the difference
        return target

def Sync(target, callback=None):
    """
    Factory function that returns a callable proxy object that can be used to
    execute a callable on the Dispatcher's main thread.
    """
    # TODO: Implement me
    pass

class DelayedTask(object):
    """
    Represents a delayed task that is scheduled to be executed once in future.

    The cancel() method of a DelayedTask instance can be used to cancel the scheduled task.
    """
    log = get_logger()
    
    def __init__(self, target, delay):
        """
        Creates a new DelayedTask that will execute the given target after the given delay (in seconds).

        To actually start the DelayedTask, you need to call the DelayedTask instance with the parameters
        that should be passed to the target.
        """
        self.target, self.delay = target, delay
        self.running = None

    def __call__(self, *args, **kwargs):
        # Bail out if this task is running or had already been run
        if self.running is not None: raise RuntimeError("This task has already been scheduled")
        self.running = True

        # Store arguments for target
        self._args, self._kwargs = args, kwargs
        # Obtain threadpool proxy in order to invoke this delayed task on the thread pool
        self.proxy = Async(self.target)
        # Schedule the delayed task call
        Dispatcher._schedule(self, self.delay)
        # Return self so that this instance can easily be stored
        return self

    def _exec(self):
        if self.running:
            # Call threadpool proxy to execute target on the threadpool
            self.proxy(*self._args, **self._kwargs)
            # We are no longer running
            self.running = False

    def cancel(self):
        """Cancels this task - the task will not be run after this method is called."""
        self.running = False

class RepeatingTask(DelayedTask):
    """
    Represents a repeating task that is scheduled to be executed at regular intervals.

    The cancel() method of a RepeatingTask instance can be used to cancel the task.
    """
    def _exec(self):
        if self.running:
            # Call threadpool proxy to execute target on the threadpool
            self.proxy(*self._args, **self._kwargs)
            # Reschedule for the next repeat
            Dispatcher._schedule(self, self.delay)
