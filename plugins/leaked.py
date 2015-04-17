
from ircstack.dispatch.events import CommandEvent, event_handler

leaked = "I found your email in the leaked data - change your Google password immediately!"
safe = "I couldn't find your email in the leaked data. Your account is probably safe."

def binsearch(data):
    fp = open('/home/terrorbite/test/google5M.txt', 'r')
    try:
        fp.seek(0, 2) # seek to EOF
        begin = 0
        end = fp.tell()

        candidate = ''
        cpos = end
        while (begin < end):
            # Jump to halfway
            origpos = ((end-begin)/2)+begin
            pos = origpos-128 if origpos>128 else 0
            fp.seek(pos, 0)
            # Search backwards until we find newline
            while pos < origpos:
                cpos = pos
                candidate = fp.readline().strip()
                pos = fp.tell()
            #if begin < cpos:
            #    return False
            #print '%30s  %30s  %s' % ('record=(%d,%d)' % (cpos, pos), 'search=(%d,%d)' % (begin, end), candidate)
            if candidate == data:
                fp.close()
                return True
            elif (candidate < data):
                begin = pos
            else:
                end = cpos
        #print "begin=%d end=%d" % (begin, end)
        return False
    finally:
        fp.close()

@event_handler(CommandEvent, command='leaked')
def on_cmd_leaked(event):
    event.reply(leaked if binsearch(event.message) else safe)
