#!/usr/bin/env python

from __future__ import generators

"""POP3DND - provides drag'n'drop training ability for POP3 clients.

This application is a twisted cross between a POP3 proxy and an IMAP
server.  It sits between your mail client and your POP3 server (like any
other POP3 proxy).  While messages classified as ham are simply passed
through the proxy, messages that are classified as spam or unsure are
intercepted and passed to the IMAP server.  The IMAP server offers three
folders - one where messages classified as spam end up, one for messages
it is unsure about, and one for training ham.

In other words, to use this application, setup your mail client to connect
to localhost, rather than directly to your POP3 server.  Additionally, add
a new IMAP account, also connecting to localhost.  Setup the application
via the web interface, and you are ready to go.  Good messages will appear
as per normal, but you will also have two new incoming folders, one for
spam and one for ham.

To train SpamBayes, use the spam folder, and the 'train_as_ham' folder.
Any messages in these folders will be trained appropriately.  This means
that all messages that SpamBayes classifies as spam will also be trained
as such.  If you receive any 'false positives' (ham classified as spam),
you *must* copy the message into the 'train_as_ham' folder to correct the
training.  You may also place any saved spam messages you have into this
folder.

So that SpamBayes knows about ham as well as spam, you will also need to
move or copy mail into the 'train_as_ham' folder.  These may come from
the unsure folder, or from any other mail you have saved.  It is a good
idea to leave messages in the 'train_as_ham' and 'spam' folders, so that
you can retrain from scratch if required.  (However, you should always
clear out your unsure folder, preferably moving or copying the messages
into the appropriate training folder).

This SpamBayes application is designed to work with Outlook Express, and
provide the same sort of ease of use as the Outlook plugin.  Although the
majority of development and testing has been done with Outlook Express,
any mail client that supports both IMAP and POP3 should be able to use this
application - if the client enables the user to work with an IMAP account
and POP3 account side-by-side (and move messages between them), then it
should work equally as well as Outlook Express.

This module includes the following classes:
 o IMAPFileMessage
 o IMAPFileMessageFactory
 o IMAPMailbox
 o SpambayesMailbox
 o Trainer
 o SpambayesAccount
 o SpambayesIMAPServer
 o OneParameterFactory
 o MyBayesProxy
 o MyBayesProxyListener
 o IMAPState
"""

todo = """
 o Message flags are currently not persisted, but should be.  The
   IMAPFileMessage class should be extended to do this.  The same
   goes for the 'internaldate' of the message.
 o The RECENT flag should be unset at some point, but when?  The
   RFC says that a message is recent if this is the first session
   to be notified about the message.  Perhaps this can be done
   simply by *not* persisting this flag - i.e. the flag is always
   loaded as not recent, and only new messages are recent.  The
   RFC says that if it is not possible to determine, then all
   messages should be recent, and this is what we currently do.
 o The Mailbox should be calling the appropriate listener
   functions (currently only newMessages is called on addMessage).
   flagsChanged should also be called on store, addMessage, or ???
 o We cannot currently get part of a message via the BODY calls
   (with the <> operands), or get a part of a MIME message (by
   prepending a number).  This should be added!
 o If the user clicks the 'save and shutdown' button on the web
   interface, this will only kill the POP3 proxy and web interface
   threads, and not the IMAP server.  We need to monitor the thread
   that we kick off, and if it dies, we should die too.  Need to figure
   out how to do this in twisted.
 o Suggestions?
"""

# This module is part of the spambayes project, which is Copyright 2002-3
# The Python Software Foundation and is covered by the Python Software
# Foundation license.

__author__ = "Tony Meyer <ta-meyer@ihug.co.nz>"
__credits__ = "All the Spambayes folk."

try:
    True, False
except NameError:
    # Maintain compatibility with Python 2.2
    True, False = 1, 0

import os
import re
import sys
import md5
import time
import errno
import types
import thread
import getopt
import imaplib
import operator
import StringIO
import email.Utils

from twisted import cred
from twisted.internet import defer
from twisted.internet import reactor
from twisted.internet.app import Application
from twisted.internet.defer import maybeDeferred
from twisted.internet.protocol import ServerFactory
from twisted.protocols.imap4 import parseNestedParens, parseIdList
from twisted.protocols.imap4 import IllegalClientResponse, IAccount
from twisted.protocols.imap4 import collapseNestedLists, MessageSet
from twisted.protocols.imap4 import IMAP4Server, MemoryAccount, IMailbox
from twisted.protocols.imap4 import IMailboxListener, collapseNestedLists

# Provide for those that don't have spambayes on their PYTHONPATH
sys.path.insert(-1, os.path.dirname(os.getcwd()))

from spambayes.Options import options
from spambayes.message import Message
from spambayes.tokenizer import tokenize
from spambayes import FileCorpus, Dibbler
from spambayes.Version import get_version_string
from spambayes.ServerUI import ServerUserInterface
from spambayes.UserInterface import UserInterfaceServer
from sb_server import POP3ProxyBase, State, _addressPortStr, _recreateState

def ensureDir(dirname):
    """Ensure that the given directory exists - in other words, if it
    does not exist, attempt to create it."""
    try:
        os.mkdir(dirname)
        if options["globals", "verbose"]:
            print "Creating directory", dirname
    except OSError, e:
        if e.errno != errno.EEXIST:
            raise


class IMAPFileMessage(FileCorpus.FileMessage):
    '''IMAP Message that persists as a file system artifact.'''

    def __init__(self, file_name, directory):
        """Constructor(message file name, corpus directory name)."""
        FileCorpus.FileMessage.__init__(self, file_name, directory)
        self.id = file_name
        self.directory = directory
        self.date = imaplib.Time2Internaldate(time.time())[1:-1]
        self.clear_flags()

    # IMessage implementation
    def getHeaders(self, negate, names):
        """Retrieve a group of message headers."""
        headers = {}
        if not isinstance(names, tuple):
            names = (names,)
        for header, value in self.items():
            if (header.upper() in names and not negate) or names == ():
                headers[header.upper()] = value
        return headers

    def getFlags(self):
        """Retrieve the flags associated with this message."""
        return self._flags_iter()

    def _flags_iter(self):    
        if self.deleted:
            yield "\\DELETED"
        if self.answered:
            yield "\\ANSWERED"
        if self.flagged:
            yield "\\FLAGGED"
        if self.seen:
            yield "\\SEEN"
        if self.draft:
            yield "\\DRAFT"
        if self.draft:
            yield "\\RECENT"

    def getInternalDate(self):
        """Retrieve the date internally associated with this message."""
        return self.date

    def getBodyFile(self):
        """Retrieve a file object containing the body of this message."""
        # Note only body, not headers!
        s = StringIO.StringIO()
        s.write(self.body())
        s.seek(0)
        return s
        #return file(os.path.join(self.directory, self.id), "r")

    def getSize(self):
        """Retrieve the total size, in octets, of this message."""
        return len(self.as_string())

    def getUID(self):
        """Retrieve the unique identifier associated with this message."""
        return self.id

    def getSubPart(self, part):
        """Retrieve a MIME sub-message
        
        @type part: C{int}
        @param part: The number of the part to retrieve, indexed from 0.
        
        @rtype: Any object implementing C{IMessage}.
        @return: The specified sub-part.
        """

    # IMessage implementation ends

    def clear_flags(self):
        """Set all message flags to false."""
        self.deleted = False
        self.answered = False
        self.flagged = False
        self.seen = False
        self.draft = False
        self.recent = False

    def set_flag(self, flag, value):
        # invalid flags are ignored
        flag = flag.upper()
        if flag == "\\DELETED":
            self.deleted = value
        elif flag == "\\ANSWERED":
            self.answered = value
        elif flag == "\\FLAGGED":
            self.flagged = value
        elif flag == "\\SEEN":
            self.seen = value
        elif flag == "\\DRAFT":
            self.draft = value
        else:
            print "Tried to set invalid flag", flag, "to", value
            
    def flags(self):
        """Return the message flags."""
        all_flags = []
        if self.deleted:
            all_flags.append("\\DELETED")
        if self.answered:
            all_flags.append("\\ANSWERED")
        if self.flagged:
            all_flags.append("\\FLAGGED")
        if self.seen:
            all_flags.append("\\SEEN")
        if self.draft:
            all_flags.append("\\DRAFT")
        if self.draft:
            all_flags.append("\\RECENT")
        return all_flags

    def train(self, classifier, isSpam):
        if self.GetTrained() == (not isSpam):
            classifier.unlearn(self.asTokens(), not isSpam)
            self.RememberTrained(None)
        if self.GetTrained() is None:
            classifier.learn(self.asTokens(), isSpam)
            self.RememberTrained(isSpam)
        classifier.store()

    def structure(self, ext=False):
        """Body structure data describes the MIME-IMB
        format of a message and consists of a sequence of mime type, mime
        subtype, parameters, content id, description, encoding, and size. 
        The fields following the size field are variable: if the mime
        type/subtype is message/rfc822, the contained message's envelope
        information, body structure data, and number of lines of text; if
        the mime type is text, the number of lines of text.  Extension fields
        may also be included; if present, they are: the MD5 hash of the body,
        body disposition, body language."""
        s = []
        for part in self.walk():
            if part.get_content_charset() is not None:
                charset = ("charset", part.get_content_charset())
            else:
                charset = None
            part_s = [part.get_main_type(), part.get_subtype(),
                      charset,
                      part.get('Content-Id'),
                      part.get('Content-Description'),
                      part.get('Content-Transfer-Encoding'),
                      str(len(part.as_string()))]
            #if part.get_type() == "message/rfc822":
            #    part_s.extend([envelope, body_structure_data,
            #                  part.as_string().count("\n")])
            #elif part.get_main_type() == "text":
            if part.get_main_type() == "text":
                part_s.append(str(part.as_string().count("\n")))
            if ext:
                part_s.extend([md5.new(part.as_string()).digest(),
                               part.get('Content-Disposition'),
                               part.get('Content-Language')])
            s.append(part_s)
        if len(s) == 1:
            return s[0]
        return s

    def body(self):    
        rfc822 = self.as_string()
        bodyRE = re.compile(r"\r?\n(\r?\n)(.*)",
                            re.DOTALL + re.MULTILINE)
        bmatch = bodyRE.search(rfc822)
        return bmatch.group(2)

    def headers(self):
        rfc822 = self.as_string()
        bodyRE = re.compile(r"\r?\n(\r?\n)(.*)",
                            re.DOTALL + re.MULTILINE)
        bmatch = bodyRE.search(rfc822)
        return rfc822[:bmatch.start(2)]

    def on(self, date1, date2):
        "contained within the date"
        raise NotImplementedError
    def before(self, date1, date2):
        "before the date"
        raise NotImplementedError
    def since(self, date1, date2):
        "within or after the date"
        raise NotImplementedError

    def string_contains(self, whole, sub):
        return whole.find(sub) != -1
        
    def matches(self, criteria):
        """Return True iff the messages matches the specified IMAP
        criteria."""
        match_tests = {"ALL" : [(True, True)],
                       "ANSWERED" : [(self.answered, True)],
                       "DELETED" : [(self.deleted, True)],
                       "DRAFT" : [(self.draft, True)],
                       "FLAGGED" : [(self.flagged, True)],
                       "NEW" : [(self.recent, True), (self.seen, False)],
                       "RECENT" : [(self.recent, True)],
                       "SEEN" : [(self.seen, True)],
                       "UNANSWERED" : [(self.answered, False)],
                       "UNDELETED" : [(self.deleted, False)],
                       "UNDRAFT" : [(self.draft, False)],
                       "UNFLAGGED" : [(self.flagged, False)],
                       "UNSEEN" : [(self.seen, False)],
                       "OLD" : [(self.recent, False)],
                       }
        complex_tests = {"BCC" : (self.string_contains, self.get("Bcc")),
                         "SUBJECT" : (self.string_contains, self.get("Subject")),
                         "CC" : (self.string_contains, self.get("Cc")),
                         "BODY" : (self.string_contains, self.body()),
                         "TO" : (self.string_contains, self.get("To")),
                         "TEXT" : (self.string_contains, self.as_string()),
                         "FROM" : (self.string_contains, self.get("From")),
                         "SMALLER" : (operator.lt, len(self.as_string())),
                         "LARGER" : (operator.gt, len(self.as_string())),
                         "BEFORE" : (self.before, self.date),
                         "ON" : (self.on, self.date),
                         "SENTBEFORE" : (self.before, self.get("Date")),
                         "SENTON" : (self.on, self.get("Date")),
                         "SENTSINCE" : (self.since, self.get("Date")),
                         "SINCE" : (self.since, self.date),
                         }
                       
        result = True
        test = None
        header = None
        header_field = None
        for c in criteria:
            if match_tests.has_key(c) and test is None and header is None:
                for (test, result) in match_tests[c]:
                    result = result and (test == result)
            elif complex_tests.has_key(c) and test is None and header is None:
                test = complex_tests[c]
            elif test is not None and header is None:
                result = result and test[0](test[1], c)
                test = None
            elif c == "HEADER" and test is None:
                # the only criteria that uses the next _two_ elements
                header = c
            elif test is None and header is not None and header_field is None:
                header_field = c
            elif header is not None and header_field is not None and test is None:
                result = result and self.string_contains(self.get(header_field), c)
                header = None
                header_field = None
        return result
"""
Still to do:
      <message set>  Messages with message sequence numbers
                     corresponding to the specified message sequence
                     number set
      UID <message set>
                     Messages with unique identifiers corresponding to
                     the specified unique identifier set.

      KEYWORD <flag> Messages with the specified keyword set.
      UNKEYWORD <flag>
                     Messages that do not have the specified keyword
                     set.

      NOT <search-key>
                     Messages that do not match the specified search
                     key.

      OR <search-key1> <search-key2>
                     Messages that match either search key.
"""


class IMAPFileMessageFactory(FileCorpus.FileMessageFactory):
    '''MessageFactory for IMAPFileMessage objects'''
    def create(self, key, directory):
        '''Create a message object from a filename in a directory'''
        return IMAPFileMessage(key, directory)


class IMAPMailbox(cred.perspective.Perspective):
    __implements__ = (IMailbox,)

    def __init__(self, name, identity_name, id):
        cred.perspective.Perspective.__init__(self, name, identity_name)
        self.UID_validity = id
        self.listeners = []

    def getUIDValidity(self):
        """Return the unique validity identifier for this mailbox."""
        return self.UID_validity

    def addListener(self, listener):
        """Add a mailbox change listener."""
        self.listeners.append(listener)
    
    def removeListener(self, listener):
        """Remove a mailbox change listener."""
        self.listeners.remove(listener)


class SpambayesMailbox(IMAPMailbox):
    def __init__(self, name, id, directory):
        IMAPMailbox.__init__(self, name, "spambayes", id)
        self.UID_validity = id
        ensureDir(directory)
        self.storage = FileCorpus.FileCorpus(IMAPFileMessageFactory(),
                                             directory, r"[0123456789]*")
        # UIDs are required to be strictly ascending.
        if len(self.storage.keys()) == 0:
            self.nextUID = 0
        else:
            self.nextUID = long(self.storage.keys()[-1]) + 1
        # Calculate initial recent and unseen counts
        # XXX Note that this will always end up with zero counts
        # XXX until the flags are persisted.
        self.unseen_count = 0
        self.recent_count = 0
        for msg in self.storage:
            if not msg.seen:
                self.unseen_count += 1
            if msg.recent:
                self.recent_count += 1
    
    def getUIDNext(self, increase=False):
        """Return the likely UID for the next message added to this
        mailbox."""
        reply = str(self.nextUID)
        if increase:
            self.nextUID += 1
        return reply

    def getUID(self, message):
        """Return the UID of a message in the mailbox."""
        # Note that IMAP messages are 1-based, our messages are 0-based
        d = self.storage
        return long(d.keys()[message - 1])

    def getFlags(self):
        """Return the flags defined in this mailbox."""
        return ["\\Answered", "\\Flagged", "\\Deleted", "\\Seen",
                "\\Draft"]

    def getMessageCount(self):
        """Return the number of messages in this mailbox."""
        return len(self.storage.keys())

    def getRecentCount(self):
        """Return the number of messages with the 'Recent' flag."""
        return self.recent_count

    def getUnseenCount(self):
        """Return the number of messages with the 'Unseen' flag."""
        return self.unseen_count
        
    def isWriteable(self):
        """Get the read/write status of the mailbox."""
        return True

    def destroy(self):
        """Called before this mailbox is deleted, permanently."""
        # Our mailboxes cannot be deleted
        raise NotImplementedError

    def getHierarchicalDelimiter(self):
        """Get the character which delimits namespaces for in this
        mailbox."""
        return '.'

    def requestStatus(self, names):
        """Return status information about this mailbox."""
        answer = {}
        for request in names:
            request = request.upper()
            if request == "MESSAGES":
                answer[request] = self.getMessageCount()
            elif request == "RECENT":
                answer[request] = self.getRecentCount()
            elif request == "UIDNEXT":
                answer[request] = self.getUIDNext()
            elif request == "UIDVALIDITY":
                answer[request] = self.getUIDValidity()
            elif request == "UNSEEN":
                answer[request] = self.getUnseenCount()
        return answer

    def addMessage(self, message, flags=(), date=None):
        """Add the given message to this mailbox."""
        msg = self.storage.makeMessage(self.getUIDNext(True))
        msg.date = date
        msg.setPayload(message.read())
        self.storage.addMessage(msg)
        self.store(MessageSet(long(msg.id), long(msg.id)), flags, 1, True)
        msg.recent = True
        msg.store()
        self.recent_count += 1
        self.unseen_count += 1

        for listener in self.listeners:
            listener.newMessages(self.getMessageCount(),
                                 self.getRecentCount())
        d = defer.Deferred()
        reactor.callLater(0, d.callback, self.storage.keys().index(msg.id))
        return d

    def expunge(self):
        """Remove all messages flagged \\Deleted."""
        deleted_messages = []
        for msg in self.storage:
            if msg.deleted:
                if not msg.seen:
                    self.unseen_count -= 1
                if msg.recent:
                    self.recent_count -= 1
                deleted_messages.append(long(msg.id))
                self.storage.removeMessage(msg)
        if deleted_messages != []:
            for listener in self.listeners:
                listener.newMessages(self.getMessageCount(),
                                     self.getRecentCount())
        return deleted_messages

    def search(self, query, uid):
        """Search for messages that meet the given query criteria.

        @type query: C{list}
        @param query: The search criteria

        @rtype: C{list}
        @return: A list of message sequence numbers or message UIDs which
        match the search criteria.
        """
        if self.getMessageCount() == 0:
            return []
        all_msgs = MessageSet(long(self.storage.keys()[0]),
                              long(self.storage.keys()[-1]))
        matches = []
        for id, msg in self._messagesIter(all_msgs, uid):
            for q in query:
                if msg.matches(q):
                    matches.append(id)
                    break
        return matches            

    def _messagesIter(self, messages, uid):
        if uid:
            messages.last = long(self.storage.keys()[-1])
        else:
            messages.last = self.getMessageCount()
        for id in messages:
            if uid:
                msg = self.storage.get(str(id))
            else:
                msg = self.storage.get(str(self.getUID(id)))
            if msg is None:
                # Non-existant message.
                continue
            msg.load()
            yield (id, msg)

    def fetch(self, messages, uid):
        """Retrieve one or more messages."""
        return self._messagesIter(messages, uid)

    def store(self, messages, flags, mode, uid):
        """Set the flags of one or more messages."""
        stored_messages = {}
        for id, msg in self._messagesIter(messages, uid):
            if mode == 0:
                msg.clear_flags()
                value = True
            elif mode == -1:
                value = False
            elif mode == 1:
                value = True
            for flag in flags:
                if flag == '(' or flag == ')':
                    continue
                if flag == "SEEN" and value == True and msg.seen == False:
                    self.unseen_count -= 1
                if flag == "SEEN" and value == False and msg.seen == True:
                    self.unseen_count += 1
                msg.set_flag(flag, value)
            stored_messages[id] = msg.flags()
        return stored_messages


class Trainer(object):
    """Listens to a given mailbox and trains new messages as spam or
    ham."""
    __implements__ = (IMailboxListener,)

    def __init__(self, mailbox, asSpam):
        self.mailbox = mailbox
        self.asSpam = asSpam

    def modeChanged(self, writeable):
        # We don't care
        pass
    
    def flagsChanged(self, newFlags):
        # We don't care
        pass

    def newMessages(self, exists, recent):
        # We don't get passed the actual message, or the id of
        # the message, of even the message number.  We just get
        # the total number of new/recent messages.
        # However, this function should be called _every_ time
        # that a new message appears, so we should be able to
        # assume that the last message is the new one.
        # (We ignore the recent count)
        if exists is not None:
            id = self.mailbox.getUID(exists)
            msg = self.mailbox.storage[str(id)]
            msg.train(state.bayes, self.asSpam)


class SpambayesAccount(MemoryAccount):
    """Account for Spambayes server."""

    def __init__(self, id, ham, spam, unsure):
        MemoryAccount.__init__(self, id)
        self.mailboxes = {"SPAM" : spam,
                          "UNSURE" : unsure,
                          "TRAIN_AS_HAM" : ham}

    def select(self, name, readwrite=1):
        # 'INBOX' is a special case-insensitive name meaning the
        # primary mailbox for the user; we interpret this as an alias
        # for 'spam'
        if name.upper() == "INBOX":
            name = "SPAM"
        return MemoryAccount.select(self, name, readwrite)


class SpambayesIMAPServer(IMAP4Server):
    IDENT = "Spambayes IMAP Server IMAP4rev1 Ready"

    def __init__(self, user_account):
        IMAP4Server.__init__(self)
        self.account = user_account

    def authenticateLogin(self, user, passwd):
        """Lookup the account associated with the given parameters."""
        if user == options["imapserver", "username"] and \
           passwd == options["imapserver", "password"]:
            return (IAccount, self.account, None)
        raise cred.error.UnauthorizedLogin()

    def connectionMade(self):
        state.activeIMAPSessions += 1
        state.totalIMAPSessions += 1
        IMAP4Server.connectionMade(self)

    def connectionLost(self, reason):
        state.activeIMAPSessions -= 1
        IMAP4Server.connectionLost(self, reason)

    def do_CREATE(self, tag, args):
        """Creating new folders on the server is not permitted."""
        self.sendNegativeResponse(tag, \
                                  "Creation of new folders is not permitted")
    auth_CREATE = (do_CREATE, IMAP4Server.arg_astring)
    select_CREATE = auth_CREATE

    def do_DELETE(self, tag, args):
        """Deleting folders on the server is not permitted."""
        self.sendNegativeResponse(tag, \
                                  "Deletion of folders is not permitted")
    auth_DELETE = (do_DELETE, IMAP4Server.arg_astring)
    select_DELETE = auth_DELETE


class OneParameterFactory(ServerFactory):
    """A factory that allows a single parameter to be passed to the created
    protocol."""
    def buildProtocol(self, addr):
        """Create an instance of a subclass of Protocol, passing a single
        parameter."""
        if self.parameter is not None:
            p = self.protocol(self.parameter)
        else:
            p = self.protocol()
        p.factory = self
        return p


class MyBayesProxy(POP3ProxyBase):
    """Proxies between an email client and a POP3 server, redirecting
    mail to the imap server as necessary.  It acts on the following
    POP3 commands:

     o RETR:
        o Adds the judgement header based on the raw headers and body
          of the message.
    """

    intercept_message = 'From: "Spambayes" <no-reply@localhost>\n' \
                        'Subject: Spambayes Intercept\n\nA message ' \
                        'was intercepted by Spambayes (it scored %s).\n' \
                        '\nYou may find it in the Spam or Unsure ' \
                        'folder.\n\n.\n'

    def __init__(self, clientSocket, serverName, serverPort, spam, unsure):
        POP3ProxyBase.__init__(self, clientSocket, serverName, serverPort)
        self.handlers = {'RETR': self.onRetr}
        state.totalSessions += 1
        state.activeSessions += 1
        self.isClosed = False
        self.spam_folder = spam
        self.unsure_folder = unsure

    def send(self, data):
        """Logs the data to the log file."""
        if options["globals", "verbose"]:
            state.logFile.write(data)
            state.logFile.flush()
        try:
            return POP3ProxyBase.send(self, data)
        except socket.error:
            self.close()

    def recv(self, size):
        """Logs the data to the log file."""
        data = POP3ProxyBase.recv(self, size)
        if options["globals", "verbose"]:
            state.logFile.write(data)
            state.logFile.flush()
        return data

    def close(self):
        # This can be called multiple times by async.
        if not self.isClosed:
            self.isClosed = True
            state.activeSessions -= 1
            POP3ProxyBase.close(self)

    def onTransaction(self, command, args, response):
        """Takes the raw request and response, and returns the
        (possibly processed) response to pass back to the email client.
        """
        handler = self.handlers.get(command, self.onUnknown)
        return handler(command, args, response)

    def onRetr(self, command, args, response):
        """Classifies the message.  If the result is ham, then simply
        pass it through.  If the result is an unsure or spam, move it
        to the appropriate IMAP folder."""
        # Use '\n\r?\n' to detect the end of the headers in case of
        # broken emails that don't use the proper line separators.
        if re.search(r'\n\r?\n', response):
            # Break off the first line, which will be '+OK'.
            ok, messageText = response.split('\n', 1)

            prob = state.bayes.spamprob(tokenize(messageText))
            if prob < options["Categorization", "ham_cutoff"]:
                # Return the +OK and the message with the header added.
                state.numHams += 1
                return ok + "\n" + messageText
            elif prob > options["Categorization", "spam_cutoff"]:
                dest_folder = self.spam_folder
                state.numSpams += 1
            else:
                dest_folder = self.unsure_folder
                state.numUnsure += 1
            msg = StringIO.StringIO(messageText)
            date = imaplib.Time2Internaldate(time.time())[1:-1]
            dest_folder.addMessage(msg, (), date)
            
            # We have to return something, because the client is expecting
            # us to.  We return a short message indicating that a message
            # was intercepted.
            return ok + "\n" + self.intercept_message % (prob,)
        else:
            # Must be an error response.
            return response

    def onUnknown(self, command, args, response):
        """Default handler; returns the server's response verbatim."""
        return response


class MyBayesProxyListener(Dibbler.Listener):
    """Listens for incoming email client connections and spins off
    MyBayesProxy objects to serve them.
    """

    def __init__(self, serverName, serverPort, proxyPort, spam, unsure):
        proxyArgs = (serverName, serverPort, spam, unsure)
        Dibbler.Listener.__init__(self, proxyPort, MyBayesProxy, proxyArgs)
        print 'Listener on port %s is proxying %s:%d' % \
               (_addressPortStr(proxyPort), serverName, serverPort)


class IMAPState(State):
    def __init__(self):
        State.__init__(self)

        # Set up the extra statistics.
        self.totalIMAPSessions = 0
        self.activeIMAPSessions = 0

    def buildServerStrings(self):
        """After the server details have been set up, this creates string
        versions of the details, for display in the Status panel."""
        self.serverPortString = str(self.imap_port)
        # Also build proxy strings
        State.buildServerStrings(self)

state = IMAPState()

# ===================================================================
# __main__ driver.
# ===================================================================

def setup():
    # Setup app, boxes, trainers and account
    proxyListeners = []
    app = Application("SpambayesIMAPServer")

    spam_box = SpambayesMailbox("Spam", 0, options["imapserver",
                                                   "spam_directory"])
    unsure_box = SpambayesMailbox("Unsure", 1, options["imapserver",
                                                       "unsure_directory"])
    ham_train_box = SpambayesMailbox("TrainAsHam", 2,
                                     options["imapserver", "ham_directory"])

    spam_trainer = Trainer(spam_box, True)
    ham_trainer = Trainer(ham_train_box, False)
    spam_box.addListener(spam_trainer)
    ham_train_box.addListener(ham_trainer)

    user_account = SpambayesAccount(options["imapserver", "username"],
                                    ham_train_box, spam_box, unsure_box)

    # add IMAP4 server
    f = OneParameterFactory()
    f.protocol = SpambayesIMAPServer
    f.parameter = user_account
    state.imap_port = options["imapserver", "port"]
    app.listenTCP(state.imap_port, f)

    # add POP3 proxy
    state.createWorkers()
    for (server, serverPort), proxyPort in zip(state.servers,
                                               state.proxyPorts):
        listener = MyBayesProxyListener(server, serverPort, proxyPort,
                                        spam_box, unsure_box)
        proxyListeners.append(listener)
    state.buildServerStrings()

    # add web interface
    httpServer = UserInterfaceServer(state.uiPort)
    serverUI = ServerUserInterface(state, _recreateState)
    httpServer.register(serverUI)

    return app    

def run():
    # Read the arguments.
    try:
        opts, args = getopt.getopt(sys.argv[1:], 'hbd:D:u:')
    except getopt.error, msg:
        print >>sys.stderr, str(msg) + '\n\n' + __doc__
        sys.exit()

    launchUI = False
    for opt, arg in opts:
        if opt == '-h':
            print >>sys.stderr, __doc__
            sys.exit()
        elif opt == '-b':
            launchUI = True
        elif opt == '-d':   # dbm file
            state.useDB = True
            options["Storage", "persistent_storage_file"] = arg
        elif opt == '-D':   # pickle file
            state.useDB = False
            options["Storage", "persistent_storage_file"] = arg
        elif opt == '-u':
            state.uiPort = int(arg)

    # Let the user know what they are using...
    print get_version_string("IMAP Server")
    print get_version_string("POP3 Proxy")
    print "and engine %s," % (get_version_string(),)
    from twisted.copyright import version as twisted_version
    print "with twisted version %s.\n" % (twisted_version,)

    # setup everything
    app = setup()

    # kick things off
    thread.start_new_thread(Dibbler.run, (launchUI,))
    app.run(save=False)

if __name__ == "__main__":
    run()