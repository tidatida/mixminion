# Copyright 2002 Nick Mathewson.  See LICENSE for licensing information.
# $Id: Queue.py,v 1.9 2002/08/12 18:12:24 nickm Exp $

"""mixminion.Queue

   Facility for a fairly secure, directory-based, unordered queue.
   """

import os
import base64
import time
import stat
import cPickle

from mixminion.Common import MixError, MixFatalError, secureDelete, getLog
from mixminion.Crypto import AESCounterPRNG

__all__ = [ 'Queue' ]

# Mode to pass to open(2) for creating a new file, and dying if it already
# exists.
_NEW_MESSAGE_MODE = os.O_WRONLY+os.O_CREAT+os.O_EXCL
# On windows or mac, binary != text.
_NEW_MESSAGE_MODE += getattr(os, 'O_BINARY', 0)

# Any inp_* files older than INPUT_TIMEOUT seconds old are assumed to be
# trash.
INPUT_TIMEOUT = 600

# If we've been cleaning for more than CLEAN_TIMEOUT seconds, assume the 
# old clean is dead.
CLEAN_TIMEOUT = 60

class Queue:
    """A Queue is an unordered collection of files with secure remove and
       move operations.

       Implementation: a queue is a directory of 'messages'.  Each
       filename in the directory has a name in one of the following
       formats:

             rmv_HANDLE   (A message waiting to be deleted)
             msg_HANDLE  (A message waiting in the queue.
             inp_HANDLE  (An incomplete message being created.)
       (Where HANDLE is a randomly chosen 12-character selection from the
       characters 'A-Za-z0-9+-'.  [Collision probability is negligable.])
       """
       # How negligible?  A back-of-the-envelope approximation: The chance
       # of a collision reaches .1% when you have 3e9 messages in a single
       # queue.  If Alice somehow manages to accumulate a 96 gigabyte
       # backlog, we'll have bigger problems than name collision... such
       # as the fact that most Unices behave badly when confronted with
       # 3 billion files in the same directory... or the fact that,
       # at today's processor speeds, it will take Alice 3 or 4
       # CPU-years to clear her backlog. 

    # Fields:   rng--a random number generator for creating new messages
    #                and getting a random slice of the queue.
    #           dir--the location of the queue.
    #           n_entries: the number of complete messages in the queue.
    #                 <0 if we haven't counted yet.  
    def __init__(self, location, create=0, scrub=0):
        """Creates a queue object for a given directory, 'location'.  If
           'create' is true, creates the directory if necessary.  If 'scrub'
           is true, removes any incomplete or invalidated messages from the
           Queue."""

        secureDelete([]) # Make sure secureDelete is configured. HACK!
        
        self.rng = AESCounterPRNG()
        self.dir = location

        if not os.path.isabs(location):
            getLog().warn("Queue path %s isn't absolute.", location)

        if os.path.exists(location) and not os.path.isdir(location):
            raise MixFatalError("%s is not a directory" % location)

        if not os.path.exists(location):
            if create:
                getLog().info("Trying to create queue %s", location)
                os.mkdir(location, 0700)
            else:
                raise MixFatalError("No directory for queue %s" % location)
 
        # Check permissions
        mode = os.stat(location)[stat.ST_MODE]
        if mode & 0077:
            # XXXX be more Draconian.
            getLog().warn("Worrisome mode %o on directory %s", mode, location)

        if scrub:
            self.cleanQueue(1)

	# Count messages on first time through.
	self.n_entries = -1

    def queueMessage(self, contents):
        """Creates a new message in the queue whose contents are 'contents',
           and returns a handle to that message."""
        f, handle = self.openNewMessage()
        f.write(contents)
        self.finishMessage(f, handle)
        return handle

    def queueObject(self, object):
	"""Queue an object using cPickle, and return a handle to that 
	   object."""
        f, handle = self.openNewMessage()
        cPickle.dump(contents, f, 1)
        self.finishMessage(f, handle)
        return handle
      
    def count(self, recount=0):
        """Returns the number of complete messages in the queue."""
	if self.n_entries >= 0 and not recount:
	    return self.n_entries
	else:
	    res = 0
	    for fn in os.listdir(self.dir):
		if fn.startswith("msg_"):
		    res += 1
	    self.n_entries = res
	    return res

    def pickRandom(self, count=None):
        """Returns a list of 'count' handles to messages in this queue.
           The messages are chosen randomly, and returned in a random order.

           If there are fewer than 'count' messages in the queue, all the
           messages will be retained."""
        messages = [fn for fn in os.listdir(self.dir) if fn.startswith("msg_")]

        n = len(messages)
        if count is None:
            count = n
        else:
            count = min(count, n)

        # This permutation algorithm yields all permutation with equal
        # probability (assuming a good rng); others do not.
        for i in range(count-1):
            swap = i+self.rng.getInt(n-i-1)
            v = messages[swap]
            messages[swap] = messages[i]
            messages[i] = v

        return [m[4:] for m in messages[:count]]

    def getAllMessages(self):
	"""Returns handles for all messages currently in the queue.
           Note: this ordering is not guaranteed to be random"""
        return [fn[4:] for fn in os.listdir(self.dir) if fn.startswith("msg_")]

    def removeMessage(self, handle):
        """Given a handle, removes the corresponding message from the queue."""
        self.__changeState(handle, "msg", "rmv")

    def removeAll(self):
        """Removes all messages from this queue."""
        removed = []
        for m in os.listdir(self.dir):
            if m[:4] in ('inp_', 'msg_'):
                self.__changeState(m[4:], m[:3], "rmv")
	self.n_entries = 0
        self.cleanQueue()

    def moveMessage(self, handle, queue):
        """Given a handle and a queue, moves the corresponding message from
           this queue to the queue provided.  Returns a new handle for
           the message in the destination queue."""
        # Since we're switching handle, we don't want to just rename;
        # We really want to copy and delete the old file.
        newHandle = queue.queueMessage(self.messageContents(handle))
        self.removeMessage(handle)
        return newHandle

    def openMessage(self, handle):
        """Given a handle for an existing message, returns a file descriptor
           open to read that message."""
        return open(os.path.join(self.dir, "msg_"+handle), 'rb')

    def messageContents(self, handle):
        """Given a message handle, returns the contents of the corresponding
           message."""
        f = open(os.path.join(self.dir, "msg_"+handle), 'rb')
        s = f.read()
        f.close()
        return s

    def getObject(self, handle):
        """Given a message handle, read and unpickle the contents of the
	   corresponding message."""
        f = open(os.path.join(self.dir, "msg_"+handle), 'rb')
        res = cPickle.load(f)
        f.close()
        return res

    def openNewMessage(self):
        """Returns (file, handle) tuple to create a new message.  Once
           you're done writing, you must call finishMessage to
           commit your changes, or abortMessage to reject them."""
        handle = self.__newHandle()
        fname = os.path.join(self.dir, "inp_"+handle)
        fd = os.open(fname, _NEW_MESSAGE_MODE, 0600)
        return os.fdopen(fd, 'wb'), handle

    def finishMessage(self, f, handle):
        """Given a file and a corresponding handle, closes the file
           commits the corresponding message."""
        f.close()
        self.__changeState(handle, "inp", "msg")

    def abortMessage(self, f, handle):
        """Given a file and a corresponding handle, closes the file
           rejects the corresponding message."""
        f.close()
        self.__changeState(handle, "inp", "rmv")

    def cleanQueue(self):
        """Removes all timed-out or trash messages from the queue.

           Returns 1 if a clean is already in progress; otherwise
           returns 0.
        """
        now = time.time() 
        cleanFile = os.path.join(self.dir,".cleaning")
        try:
            s = os.stat(cleanFile)
            if now - s[stat.ST_MTIME] > CLEAN_TIMEOUT:
                cleaning = 0
            cleaning = 1    
        except OSError:
            cleaning = 0

        if cleaning:
            return 1

        f = open(cleanFile, 'w')
        f.write(str(now))
        f.close()
        
        rmv = []
        allowedTime = int(time.time()) - INPUT_TIMEOUT
        for m in os.listdir(self.dir):
            if m.startswith("rmv_"):
                rmv.append(os.path.join(self.dir, m))
            elif m.startswith("inp_"):
                s = os.stat(m)
                if s[stat.ST_MTIME] < allowedTime:
                    self.__changeState(m[4:], "inp", "rmv")
                    rmv.append(os.path.join(self.dir, m))
        _secureDelete_bg(rmv, cleanFile)
        return 0

    def __changeState(self, handle, s1, s2):
        """Helper method: changes the state of message 'handle' from 's1'
           to 's2', and changes the internal count."""
        os.rename(os.path.join(self.dir, s1+"_"+handle),
                  os.path.join(self.dir, s2+"_"+handle))
	if self.n_entries < 0:
	    return
	if s1 == 'msg' and s2 != 'msg':
	    self.n_entries -= 1
	elif s1 != 'msg' and s2 == 'msg':
	    self.n_entries += 1

    def __newHandle(self):
        """Helper method: creates a new random handle."""
        junk = self.rng.getBytes(9)
        return base64.encodestring(junk).strip().replace("/","-")

class DeliveryQueue(Queue):
    """A DeliveryQueue implements a queue that greedily sends messages
       to outgoing streams that occasionally fail.  Messages in a 
       DeliveryQueue are no longer unstructured text, but rather
       tuples of: (n_retries, next_retry_time, addressing info, msg).

       This class is abstract. Implementors of this class should
       subclass it to add a deliverMessages method.  Multiple
       invocations of this method may be active at a given time.  Upon
       success or failure, this method should cause deliverySucceeded
       or deliveryFailed to be called as appropriate.

       Users of this class will probably only want to call the queueMessage,
       sendReadyMessages, and nextMessageReadyAt methods.

       This class caches information about the directory state; it
       won't play nice if multiple instances are looking at the same
       directory.
    """
    ###
    # Fields:
    #    sendableAt -- An inorder list of (time, handle) for all messages 
    #           that we're not currently sending. Each time is either a
    #           number of seconds since the epoch at which the corresponding
    #           message will be sendable, or 0 to indicate that the message
    #           is sendable _now_.  
    #    pending -- Dict from handle->1, for all messages that we're
    #           currently sending.

    def __init__(self, location):
	Queue.__init__(self, location, create=1, scrub=1)
	self._rescan()

    def _rescan(self):
	"""Rebuild the internal state of this queue from the underlying
           directory."""
	self.sendableAt = []
	self.pending = {}
	now = time.time()
	for h in self.getAllmessages():
	    t = self.getObject(handle)[1] # retry time
	    self.sendableAt.append[(t, h)]
	self.sendableAt.sort()
    
    def queueMessage(self, addr, msg, retry=0, retryAt=0):
	"""Schedule a message for delivery.
	     addr -- An object to indicate the message's destination
	     msg -- the message itself
	     retry -- how many times so far have we tried to send?
	     retryAt -- when should we next retry? (0 for now)."""

        self.queueObject( (retry, retryAt, addr, msg) )

	if retryAt == 0:
	    self.sendableAt[0:0] = [(0, handle)]
	else:
	    bisect.insort_right(self.sendableAt, (retryAt, handle))
	return handle

    def nextMessageReadyAt(self):
	"""Return the soonest possible time at which sendReadyMessages
	   will send something.  If some time < now is returned,
	   the answer is 'immediately'. """
	if self.sendableAt:
	    return self.sendableAt[0][0]
	else:
	    return None
    
    def messageContents(self,handle):
	"""Returns a (n_retries, retryAt, addr, msg) payload for a given
	   message handle."""
        return self.getObject(handle)
	
    def sendReadyMessages(self):
	"""Sends all messages which are ready to be send, and which are not
           already being sent."""
	now = time.time()
	idx = bisect.bisect_right(self.sendableAt, (now, ""))
	messages = []
	sendable = self.sendableAt[:idx]
	del self.sendableAt[:idx]
	for when, h in self.sendable:
	    retries, retryAt, addr, msg = self.getObject(h)
	    messages.append((h, addr, msg, retries))
	    self.pending[h] = 1
	if messages:
	    self.deliverMessage(messages)

    def deliverMessages(self, msgList):
	"""Abstract method; Invoked with a list of  
  	   (handle, addr, message, n_retries) tuples every time we have a batch
	   of messages to send."""
        # We could implement this as a single deliverMessage(h,addr,m,n)
	# method, but that wouldn't allow implementations to batch
	# messages being sent to the same address.
	assert 0

    def deliverySucceeded(self, handle):
	"""Removes a message from the outgoing queue.  This method
	   should be invoked after the corresponding message has been
	   successfully delivered.
        """
	self.removeMessage(handle)
	del self.pending[handle]

    def deliveryFailed(self, handle, retryAt=None):
	"""Removes a message from the outgoing queue, or requeues it
	   for delivery at a later time.  This method should be
	   invoked after the corresponding message has been
	   successfully delivered."""
	del self.pending[handle]
	if self.retryAt is not None:
	    # Queue the new one before removing the old one, for 
	    # crash-proofness
	    retries, retryAt, addr, msg = self.getObject(h)
	    self.queueMessage(addr, msg, retries+1, retryAt)
	self.removeMessage(handle)	    

class MixQueue(Queue):
    """A MixQueue holds a group of files, and returns some of them
       as requested, according to some mixing algorithm.
       
       It's the responsibility of the user of this class to only invoke it
       at the specified interval."""
    # Right now, we use the 'Cottrell' mixing algorithm with fixed
    # parameters.  We always keep 5 messages in the pool, and never send
    # more than 30% of the pool at a time.  These parameters should probably
    # be more like 100
    MIN_POOL_SIZE = 5
    MAX_REPLACEMENT_RATE = 0.3
    def __init__(self, location):
	Queue.__init__(self, location, create=1, scrub=1)

    def pickMessages(self):
	"""Return a list of handles."""
	n = self.count()
	nTransmit = min(n-self.MIN_POOL_SIZE,
		       int(n*self.MAX_REPLACEMENT_RATE))
	return self.pickRandom(nTransmit)

def _secureDelete_bg(files, cleanFile):
    pid = os.fork()
    if pid != 0:
        return pid
    # Now we're in the child process.
    try:
        secureDelete(files, blocking=1)
    except OSError, e:
        # This is sometimes thrown when shred finishes before waitpid.
        pass
    try:
        os.unlink(cleanFile)
    except OSError, e:
        pass
    os._exit(0)
