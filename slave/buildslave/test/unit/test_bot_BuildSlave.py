# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

import os
import shutil
import socket

from twisted.trial import unittest
from twisted.spread import pb
from twisted.internet import reactor, defer
from twisted.cred import checkers, portal
from zope.interface import implements

from buildslave import bot

from mock import Mock

# I don't see any simple way to test the PB equipment without actually setting
# up a TCP connection.  This just tests that the PB code will connect and can
# execute a basic ping.  The rest is done without TCP (or PB) in other test modules.

class MasterPerspective(pb.Avatar):
    def __init__(self, on_keepalive=None):
        self.on_keepalive = on_keepalive

    def perspective_keepalive(self):
        if self.on_keepalive:
            on_keepalive, self.on_keepalive = self.on_keepalive, None
            on_keepalive()

class MasterRealm:
    def __init__(self, perspective, on_attachment):
        self.perspective = perspective
        self.on_attachment = on_attachment

    implements(portal.IRealm)
    def requestAvatar(self, avatarId, mind, *interfaces):
        assert pb.IPerspective in interfaces
        self.mind = mind
        self.perspective.mind = mind
        d = defer.succeed(None)
        if self.on_attachment:
            d.addCallback(lambda _: self.on_attachment(mind))
        def returnAvatar(_):
            return pb.IPerspective, self.perspective, lambda: None
        d.addCallback(returnAvatar)
        return d

    def shutdown(self):
        return self.mind.broker.transport.loseConnection()

class TestBuildSlave(unittest.TestCase):

    def setUp(self):
        self.realm = None
        self.buildslave = None
        self.listeningport = None

        self.basedir = os.path.abspath("basedir")
        if os.path.exists(self.basedir):
            shutil.rmtree(self.basedir)
        os.makedirs(self.basedir)

        # the slave tries to call socket.getfqdn to write its hostname; this hangs
        # without network, so fake it
        self.patch(socket, "getfqdn", lambda : 'test-hostname.domain.com')

    def tearDown(self):
        d = defer.succeed(None)
        if self.realm:
            d.addCallback(lambda _ : self.realm.shutdown())
        if self.buildslave and self.buildslave.running:
            d.addCallback(lambda _ : self.buildslave.stopService())
        if self.listeningport:
            d.addCallback(lambda _ : self.listeningport.stopListening())
        if os.path.exists(self.basedir):
            shutil.rmtree(self.basedir)
        return d

    def start_master(self, perspective, on_attachment=None):
        self.realm = MasterRealm(perspective, on_attachment)
        p = portal.Portal(self.realm)
        p.registerChecker(
            checkers.InMemoryUsernamePasswordDatabaseDontUse(testy="westy"))
        self.listeningport = reactor.listenTCP(0, pb.PBServerFactory(p), interface='127.0.0.1')
        # return the dynamically allocated port number
        return self.listeningport.getHost().port

    def test_keepalive_called(self):
        # set up to fire this deferred on receipt of a keepalive
        d = defer.Deferred()
        def on_keepalive():
            # need to wait long enough for the remote_keepalive call to
            # finish, but not for another one to queue up
            reactor.callLater(0.01, d.callback, None)
        persp = MasterPerspective(on_keepalive=on_keepalive)

        # start up the master and slave, with a very short keepalive
        port = self.start_master(persp)
        self.buildslave = bot.BuildSlave("127.0.0.1", port,
                "testy", "westy", self.basedir,
                keepalive=0.1, keepaliveTimeout=0.05, usePTY=False)
        self.buildslave.startService()

        # and wait for it to keepalive
        return d

    def test_buildslave_print(self):
        d = defer.Deferred()

        # set up to call print when we are attached, and chain the results onto
        # the deferred for the whole test
        def call_print(mind):
            print_d = mind.callRemote("print", "Hi, slave.")
            print_d.addCallbacks(d.callback, d.errback)

        # start up the master and slave, with a very short keepalive
        persp = MasterPerspective()
        port = self.start_master(persp, on_attachment=call_print)
        self.buildslave = bot.BuildSlave("127.0.0.1", port,
                "testy", "westy", self.basedir,
                keepalive=0, usePTY=False, umask=022)
        self.buildslave.startService()

        # and wait for the result of the print
        return d

    def test_recordHostname(self):
        self.buildslave = bot.BuildSlave("127.0.0.1", 9999,
                "testy", "westy", self.basedir,
                keepalive=0, usePTY=False, umask=022)
        self.buildslave.recordHostname(self.basedir)
        self.assertEqual(open(os.path.join(self.basedir, "twistd.hostname")).read().strip(),
                         'test-hostname.domain.com')

    def test_buildslave_graceful_shutdown(self):
        """Test that running the build slave's gracefulShutdown method results
        in a call to the master's shutdown method"""
        d = defer.Deferred()

        fakepersp = Mock()
        called = []
        def fakeCallRemote(*args):
            called.append(args)
            d1 = defer.succeed(None)
            return d1
        fakepersp.callRemote = fakeCallRemote

        # set up to call shutdown when we are attached, and chain the results onto
        # the deferred for the whole test
        def call_shutdown(mind):
            self.buildslave.bf.perspective = fakepersp
            shutdown_d = self.buildslave.gracefulShutdown()
            shutdown_d.addCallbacks(d.callback, d.errback)

        persp = MasterPerspective()
        port = self.start_master(persp, on_attachment=call_shutdown)

        self.buildslave = bot.BuildSlave("127.0.0.1", port,
                "testy", "westy", self.basedir,
                keepalive=0, usePTY=False, umask=022)

        self.buildslave.startService()

        def check(ign):
            self.assertEquals(called, [('shutdown',)])
        d.addCallback(check)

        return d

    def test_buildslave_shutdown(self):
        """Test watching an existing shutdown_file results in gracefulShutdown
        being called."""

        buildslave = bot.BuildSlave("127.0.0.1", 1234,
                "testy", "westy", self.basedir,
                keepalive=0, usePTY=False, umask=022,
                allow_shutdown='file')

        # Mock out gracefulShutdown
        buildslave.gracefulShutdown = Mock()

        # Mock out os.path methods
        exists = Mock()
        mtime = Mock()

        self.patch(os.path, 'exists', exists)
        self.patch(os.path, 'getmtime', mtime)

        # Pretend that the shutdown file doesn't exist
        mtime.return_value = 0
        exists.return_value = False

        buildslave._checkShutdownFile()

        # We shouldn't have called gracefulShutdown
        self.assertEquals(buildslave.gracefulShutdown.call_count, 0)

        # Pretend that the file exists now, with an mtime of 2
        exists.return_value = True
        mtime.return_value = 2
        buildslave._checkShutdownFile()

        # Now we should have changed gracefulShutdown
        self.assertEquals(buildslave.gracefulShutdown.call_count, 1)

        # Bump the mtime again, and make sure we call shutdown again
        mtime.return_value = 3
        buildslave._checkShutdownFile()
        self.assertEquals(buildslave.gracefulShutdown.call_count, 2)

        # Try again, we shouldn't call shutdown another time
        buildslave._checkShutdownFile()
        self.assertEquals(buildslave.gracefulShutdown.call_count, 2)
