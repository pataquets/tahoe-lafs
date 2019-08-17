from os.path import join
from twisted.trial import unittest
from twisted.internet import reactor
from foolscap.api import fireEventually, flushEventualQueue
from twisted.internet import defer
from allmydata.introducer import create_introducer
from allmydata import node
from .common import (
    FAVICON_MARKUP,
)
from ..common import (
    SameProcessStreamEndpointAssigner,
)
from ..common_web import do_http

class IntroducerWeb(unittest.TestCase):
    def setUp(self):
        self.node = None
        self.port_assigner = SameProcessStreamEndpointAssigner()
        self.port_assigner.setUp()
        self.addCleanup(self.port_assigner.tearDown)

    def tearDown(self):
        d = defer.succeed(None)
        if self.node:
            d.addCallback(lambda ign: self.node.stopService())
        d.addCallback(flushEventualQueue)
        return d

    @defer.inlineCallbacks
    def test_welcome(self):
        basedir = self.mktemp()
        node.create_node_dir(basedir, "testing")
        _, port_endpoint = self.port_assigner.assign(reactor)
        with open(join(basedir, "tahoe.cfg"), "w") as f:
            f.write(
                "[node]\n"
                "tub.location = 127.0.0.1:1\n" +
                "web.port = {}\n".format(port_endpoint)
            )

        self.node = yield create_introducer(basedir)
        self.ws = self.node.getServiceNamed("webish")

        yield fireEventually(None)
        self.node.startService()

        url = "http://localhost:%d/" % self.ws.getPortnum()
        res = yield do_http("get", url)
        self.failUnlessIn('Welcome to the Tahoe-LAFS Introducer', res)
        self.failUnlessIn(FAVICON_MARKUP, res)
        self.failUnlessIn('Page rendered at', res)
        self.failUnlessIn('Tahoe-LAFS code imported from:', res)
