"""
This module contains utilities for parsing the IRC Client-To-Client
Protocol (CTCP) as laid out in the document at the following URL:
    http://www.irchelp.org/irchelp/rfc/ctcpspec.html

Note that this standard is considered archaic and is rarely used in practice.
"""

import re
re_ctcp = re.compile('\001([^\001]+)\001')

# The low-level quote character, M-QUOTE, is \020

def low_quote(data):
    """
    Performs low-level quoting of IRC data.
    """
    # Easy method (probably not very performant though)
    return data.replace(u'\020', u'\020\020').replace(u'\000', u'\0200') \
            .replace(u'\n', u'\020n').replace(u'\r', u'\020r')

mquotes = {u'0': u'\0', u'n': u'\n', u'r': u'\r', u'\020': u'\020'}
def low_dequote(data):
    """
    Performs low-level dequoting of IRC data.
    """
    output = u''
    start = 0
    # Abusing list comprehension to assign in an expression.
    # equivalent of C code: while( (result = data.find(...)) >= 0)
    while [result for result in [ data.find(u'\020', start) ] if result >= 0]:
        #if start == result: break
        output += data[start:result]
        nextchar = data[result+1]
        if nextchar in mquotes:
            output += mquotes[nextchar]
            start = result+2
        else:
            # invalid quote sequence, remove M-QUOTE and ignore
            start = result+1
    output += data[start:]
    return output

# X-QUOTE is \134 - i.e. a backslash

def ctcp_quote(data):
    r"""
    Performs quoting of CTCP data (i.e. data between \001 delimiters).
    """
    return data.replace(u'\\', u'\\\\').replace(u'\001', u'\\a')

def ctcp_dequote(data):
    """
    Performs dequoting of CTCP data.
    """
    return data.replace(u'\\a', u'\001').replace(u'\\\\', u'\\').replace(u'\\', u'')

def ctcp_extract(data):
    """
    Extracts CTCP messages from a string.
    The returned CTCP messages, if any, are dequoted for you.
    """
    return [ctcp_dequote(msg) for msg in re_ctcp.findall(data)]

def ctcp_strip(data):
    """
    Strips CTCP messages from a string.
    """
    return re_ctcp.sub(u'', data)

def ctcp_create(tag, data=None):
    """
    Builds a CTCP message with the given tag and optional data.
    """ 
    return u"\001%s\001" % ctcp_quote( (u"%s %s" % (tag, data)) if data else tag )

