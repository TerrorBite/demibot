import sys

py3 = (sys.version_info[0] == 3)

unicode = None
if py3 and 'str' in __builtins__: unicode = __builtins__['str']
elif hasattr(__builtins__, 'unicode'): unicode = __builtins__.unicode
if unicode is None:
    print(repr(__builtins__.keys()))
    raise ValueError('unicode is None')
str = bytes # in python2.6, this is already str
