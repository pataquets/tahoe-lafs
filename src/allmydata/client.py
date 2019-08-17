import os, stat, time, weakref
from base64 import urlsafe_b64encode
from functools import partial
from errno import ENOENT, EPERM

from zope.interface import implementer
from twisted.internet import reactor, defer
from twisted.application import service
from twisted.application.internet import TimerService
from twisted.python.filepath import FilePath

import allmydata
from allmydata.crypto import rsa, ed25519
from allmydata.crypto.util import remove_prefix
from allmydata.storage.server import StorageServer
from allmydata import storage_client
from allmydata.immutable.upload import Uploader
from allmydata.immutable.offloaded import Helper
from allmydata.control import ControlServer
from allmydata.introducer.client import IntroducerClient
from allmydata.util import (hashutil, base32, pollmixin, log, idlib, yamlutil)
from allmydata.util.encodingutil import (get_filesystem_encoding,
                                         from_utf8_or_none)
from allmydata.util.abbreviate import parse_abbreviated_size
from allmydata.util.time_format import parse_duration, parse_date
from allmydata.util.i2p_provider import create as create_i2p_provider
from allmydata.util.tor_provider import create as create_tor_provider
from allmydata.stats import StatsProvider
from allmydata.history import History
from allmydata.interfaces import IStatsProducer, SDMF_VERSION, MDMF_VERSION, DEFAULT_MAX_SEGMENT_SIZE
from allmydata.nodemaker import NodeMaker
from allmydata.blacklist import Blacklist
from allmydata import node


KiB=1024
MiB=1024*KiB
GiB=1024*MiB
TiB=1024*GiB
PiB=1024*TiB

def _valid_config_sections():
    cfg = node._common_config_sections()
    cfg.update({
        "client": (
            "helper.furl",
            "introducer.furl",
            "key_generator.furl",
            "mutable.format",
            "peers.preferred",
            "shares.happy",
            "shares.needed",
            "shares.total",
            "stats_gatherer.furl",
        ),
        "drop_upload": (  # deprecated already?
            "enabled",
        ),
        "ftpd": (
            "accounts.file",
            "accounts.url",
            "enabled",
            "port",
        ),
        "storage": (
            "debug_discard",
            "enabled",
            "expire.cutoff_date",
            "expire.enabled",
            "expire.immutable",
            "expire.mode",
            "expire.mode",
            "expire.mutable",
            "expire.override_lease_duration",
            "readonly",
            "reserved_space",
            "storage_dir",
        ),
        "sftpd": (
            "accounts.file",
            "accounts.url",
            "enabled",
            "host_privkey_file",
            "host_pubkey_file",
            "port",
        ),
        "helper": (
            "enabled",
        ),
        "magic_folder": (
            "download.umask",
            "enabled",
            "local.directory",
            "poll_interval",
        ),
    })
    return cfg

# this is put into README in new node-directories
CLIENT_README = """
This directory contains files which contain private data for the Tahoe node,
such as private keys.  On Unix-like systems, the permissions on this directory
are set to disallow users other than its owner from reading the contents of
the files.   See the 'configuration.rst' documentation file for details.
"""



def _make_secret():
    """
    Returns a base32-encoded random secret of hashutil.CRYPTO_VAL_SIZE
    bytes.
    """
    return base32.b2a(os.urandom(hashutil.CRYPTO_VAL_SIZE)) + "\n"


class SecretHolder(object):
    def __init__(self, lease_secret, convergence_secret):
        self._lease_secret = lease_secret
        self._convergence_secret = convergence_secret

    def get_renewal_secret(self):
        return hashutil.my_renewal_secret_hash(self._lease_secret)

    def get_cancel_secret(self):
        return hashutil.my_cancel_secret_hash(self._lease_secret)

    def get_convergence_secret(self):
        return self._convergence_secret

class KeyGenerator(object):
    """I create RSA keys for mutable files. Each call to generate() returns a
    single keypair. The keysize is specified first by the keysize= argument
    to generate(), then with a default set by set_default_keysize(), then
    with a built-in default of 2048 bits."""
    def __init__(self):
        self.default_keysize = 2048

    def set_default_keysize(self, keysize):
        """Call this to override the size of the RSA keys created for new
        mutable files which don't otherwise specify a size. This will affect
        all subsequent calls to generate() without a keysize= argument. The
        default size is 2048 bits. Test cases should call this method once
        during setup, to cause me to create smaller keys, so the unit tests
        run faster."""
        self.default_keysize = keysize

    def generate(self, keysize=None):
        """I return a Deferred that fires with a (verifyingkey, signingkey)
        pair. I accept a keysize in bits (2048 bit keys are standard, smaller
        keys are used for testing). If you do not provide a keysize, I will
        use my default, which is set by a call to set_default_keysize(). If
        set_default_keysize() has never been called, I will create 2048 bit
        keys."""
        keysize = keysize or self.default_keysize
        # RSA key generation for a 2048 bit key takes between 0.8 and 3.2
        # secs
        signer, verifier = rsa.create_signing_keypair(keysize)
        return defer.succeed( (verifier, signer) )

class Terminator(service.Service):
    def __init__(self):
        self._clients = weakref.WeakKeyDictionary()
    def register(self, c):
        self._clients[c] = None
    def stopService(self):
        for c in self._clients:
            c.stop()
        return service.Service.stopService(self)


def read_config(basedir, portnumfile, generated_files=[]):
    """
    Read and validate configuration for a client-style Node. See
    :method:`allmydata.node.read_config` for parameter meanings (the
    only difference here is we pass different validation data)

    :returns: :class:`allmydata.node._Config` instance
    """
    return node.read_config(
        basedir, portnumfile,
        generated_files=generated_files,
        _valid_config_sections=_valid_config_sections,
    )


def create_client(basedir=u".", _client_factory=None):
    """
    Creates a new client instance (a subclass of Node).

    :param unicode basedir: the node directory (which may not exist yet)

    :param _client_factory: (for testing) a callable that returns an
        instance of :class:`allmydata.node.Node` (or a subclass). By default
        this is :class:`allmydata.client._Client`

    :returns: Deferred yielding an instance of :class:`allmydata.client._Client`
    """
    try:
        node.create_node_dir(basedir, CLIENT_README)
        config = read_config(basedir, u"client.port")
        # following call is async
        return create_client_from_config(
            config,
            _client_factory=_client_factory,
        )
    except Exception:
        return defer.fail()


def create_client_from_config(config, _client_factory=None):
    """
    Creates a new client instance (a subclass of Node).  Most code
    should probably use `create_client` instead.

    :returns: Deferred yielding a _Client instance

    :param config: configuration instance (from read_config()) which
        encapsulates everything in the "node directory".

    :param _client_factory: for testing; the class to instantiate
        instead of _Client
    """
    try:
        if _client_factory is None:
            _client_factory = _Client

        i2p_provider = create_i2p_provider(reactor, config)
        tor_provider = create_tor_provider(reactor, config)
        handlers = node.create_connection_handlers(reactor, config, i2p_provider, tor_provider)
        default_connection_handlers, foolscap_connection_handlers = handlers
        tub_options = node.create_tub_options(config)

        main_tub = node.create_main_tub(
            config, tub_options, default_connection_handlers,
            foolscap_connection_handlers, i2p_provider, tor_provider,
        )
        control_tub = node.create_control_tub()

        introducer_clients = create_introducer_clients(config, main_tub)
        storage_broker = create_storage_farm_broker(
            config, default_connection_handlers, foolscap_connection_handlers,
            tub_options, introducer_clients
        )

        client = _client_factory(
            config,
            main_tub,
            control_tub,
            i2p_provider,
            tor_provider,
            introducer_clients,
            storage_broker,
        )
        i2p_provider.setServiceParent(client)
        tor_provider.setServiceParent(client)
        for ic in introducer_clients:
            ic.setServiceParent(client)
        storage_broker.setServiceParent(client)
        return defer.succeed(client)
    except Exception:
        return defer.fail()


def _sequencer(config):
    """
    :returns: a 2-tuple consisting of a new announcement
        sequence-number and random nonce (int, unicode). Reads and
        re-writes configuration file "announcement-seqnum" (starting at 1
        if that file doesn't exist).
    """
    seqnum_s = config.get_config_from_file("announcement-seqnum")
    if not seqnum_s:
        seqnum_s = u"0"
    seqnum = int(seqnum_s.strip())
    seqnum += 1  # increment
    config.write_config_file("announcement-seqnum", "{}\n".format(seqnum))
    nonce = _make_secret().strip()
    return seqnum, nonce


def create_introducer_clients(config, main_tub):
    """
    Read, validate and parse any 'introducers.yaml' configuration.

    :returns: a list of IntroducerClient instances
    """
    # we return this list
    introducer_clients = []

    introducers_yaml_filename = config.get_private_path("introducers.yaml")
    introducers_filepath = FilePath(introducers_yaml_filename)

    try:
        with introducers_filepath.open() as f:
            introducers_yaml = yamlutil.safe_load(f)
            if introducers_yaml is None:
                raise EnvironmentError(
                    EPERM,
                    "Can't read '{}'".format(introducers_yaml_filename),
                    introducers_yaml_filename,
                )
            introducers = introducers_yaml.get("introducers", {})
            log.msg(
                "found {} introducers in private/introducers.yaml".format(
                    len(introducers),
                )
            )
    except EnvironmentError as e:
        if e.errno != ENOENT:
            raise
        introducers = {}

    if "default" in introducers.keys():
        raise ValueError(
            "'default' introducer furl cannot be specified in introducers.yaml;"
            " please fix impossible configuration."
        )

    # read furl from tahoe.cfg
    tahoe_cfg_introducer_furl = config.get_config("client", "introducer.furl", None)
    if tahoe_cfg_introducer_furl == "None":
        raise ValueError(
            "tahoe.cfg has invalid 'introducer.furl = None':"
            " to disable it, use 'introducer.furl ='"
            " or omit the key entirely"
        )
    if tahoe_cfg_introducer_furl:
        introducers[u'default'] = {'furl':tahoe_cfg_introducer_furl}

    for petname, introducer in introducers.items():
        introducer_cache_filepath = FilePath(config.get_private_path("introducer_{}_cache.yaml".format(petname)))
        ic = IntroducerClient(
            main_tub,
            introducer['furl'].encode("ascii"),
            config.nickname,
            str(allmydata.__full_version__),
            str(_Client.OLDEST_SUPPORTED_VERSION),
            node.get_app_versions(),
            partial(_sequencer, config),
            introducer_cache_filepath,
        )
        introducer_clients.append(ic)
    return introducer_clients


def create_storage_farm_broker(config, default_connection_handlers, foolscap_connection_handlers, tub_options, introducer_clients):
    """
    Create a StorageFarmBroker object, for use by Uploader/Downloader
    (and everybody else who wants to use storage servers)

    :param config: a _Config instance

    :param default_connection_handlers: default Foolscap handlers

    :param foolscap_connection_handlers: available/configured Foolscap
        handlers

    :param dict tub_options: how to configure our Tub

    :param list introducer_clients: IntroducerClient instances if
        we're connecting to any
    """
    ps = config.get_config("client", "peers.preferred", "").split(",")
    preferred_peers = tuple([p.strip() for p in ps if p != ""])

    def tub_creator(handler_overrides=None, **kwargs):
        return node.create_tub(
            tub_options,
            default_connection_handlers,
            foolscap_connection_handlers,
            handler_overrides={} if handler_overrides is None else handler_overrides,
            **kwargs
        )

    sb = storage_client.StorageFarmBroker(
        permute_peers=True,
        tub_maker=tub_creator,
        preferred_peers=preferred_peers,
    )
    for ic in introducer_clients:
        sb.use_introducer(ic)
    return sb


@implementer(IStatsProducer)
class _Client(node.Node, pollmixin.PollMixin):

    STOREDIR = 'storage'
    NODETYPE = "client"
    EXIT_TRIGGER_FILE = "exit_trigger"

    # This means that if a storage server treats me as though I were a
    # 1.0.0 storage client, it will work as they expect.
    OLDEST_SUPPORTED_VERSION = "1.0.0"

    # This is a dictionary of (needed, desired, total, max_segment_size). 'needed'
    # is the number of shares required to reconstruct a file. 'desired' means
    # that we will abort an upload unless we can allocate space for at least
    # this many. 'total' is the total number of shares created by encoding.
    # If everybody has room then this is is how many we will upload.
    DEFAULT_ENCODING_PARAMETERS = {"k": 3,
                                   "happy": 7,
                                   "n": 10,
                                   "max_segment_size": DEFAULT_MAX_SEGMENT_SIZE,
                                   }

    def __init__(self, config, main_tub, control_tub, i2p_provider, tor_provider, introducer_clients,
                 storage_farm_broker):
        """
        Use :func:`allmydata.client.create_client` to instantiate one of these.
        """
        node.Node.__init__(self, config, main_tub, control_tub, i2p_provider, tor_provider)

        self._magic_folders = dict()
        self.started_timestamp = time.time()
        self.logSource = "Client"
        self.encoding_params = self.DEFAULT_ENCODING_PARAMETERS.copy()

        self.introducer_clients = introducer_clients
        self.storage_broker = storage_farm_broker

        self.init_stats_provider()
        self.init_secrets()
        self.init_node_key()
        self.init_storage()
        self.init_control()
        self._key_generator = KeyGenerator()
        key_gen_furl = config.get_config("client", "key_generator.furl", None)
        if key_gen_furl:
            log.msg("[client]key_generator.furl= is now ignored, see #2783")
        self.init_client()
        self.load_static_servers()
        self.helper = None
        if config.get_config("helper", "enabled", False, boolean=True):
            if not self._is_tub_listening():
                raise ValueError("config error: helper is enabled, but tub "
                                 "is not listening ('tub.port=' is empty)")
            self.init_helper()
        self.init_ftp_server()
        self.init_sftp_server()
        self.init_magic_folder()

        # If the node sees an exit_trigger file, it will poll every second to see
        # whether the file still exists, and what its mtime is. If the file does not
        # exist or has not been modified for a given timeout, the node will exit.
        exit_trigger_file = config.get_config_path(self.EXIT_TRIGGER_FILE)
        if os.path.exists(exit_trigger_file):
            age = time.time() - os.stat(exit_trigger_file)[stat.ST_MTIME]
            self.log("%s file noticed (%ds old), starting timer" % (self.EXIT_TRIGGER_FILE, age))
            exit_trigger = TimerService(1.0, self._check_exit_trigger, exit_trigger_file)
            exit_trigger.setServiceParent(self)

        # this needs to happen last, so it can use getServiceNamed() to
        # acquire references to StorageServer and other web-statusable things
        webport = config.get_config("node", "web.port", None)
        if webport:
            self.init_web(webport) # strports string

    def init_stats_provider(self):
        gatherer_furl = self.config.get_config("client", "stats_gatherer.furl", None)
        self.stats_provider = StatsProvider(self, gatherer_furl)
        self.stats_provider.setServiceParent(self)
        self.stats_provider.register_producer(self)

    def get_stats(self):
        return { 'node.uptime': time.time() - self.started_timestamp }

    def init_secrets(self):
        lease_s = self.config.get_or_create_private_config("secret", _make_secret)
        lease_secret = base32.a2b(lease_s)
        convergence_s = self.config.get_or_create_private_config('convergence',
                                                                 _make_secret)
        self.convergence = base32.a2b(convergence_s)
        self._secret_holder = SecretHolder(lease_secret, self.convergence)

    def init_node_key(self):
        # we only create the key once. On all subsequent runs, we re-use the
        # existing key
        def _make_key():
            private_key, _ = ed25519.create_signing_keypair()
            return ed25519.string_from_signing_key(private_key) + "\n"

        private_key_str = self.config.get_or_create_private_config("node.privkey", _make_key)
        private_key, public_key = ed25519.signing_keypair_from_string(private_key_str)
        public_key_str = ed25519.string_from_verifying_key(public_key)
        self.config.write_config_file("node.pubkey", public_key_str + "\n")
        self._node_private_key = private_key
        self._node_public_key = public_key

    def get_long_nodeid(self):
        # this matches what IServer.get_longname() says about us elsewhere
        vk_string = ed25519.string_from_verifying_key(self._node_public_key)
        return remove_prefix(vk_string, "pub-")

    def get_long_tubid(self):
        return idlib.nodeid_b2a(self.nodeid)

    def _init_permutation_seed(self, ss):
        seed = self.config.get_config_from_file("permutation-seed")
        if not seed:
            have_shares = ss.have_shares()
            if have_shares:
                # if the server has shares but not a recorded
                # permutation-seed, then it has been around since pre-#466
                # days, and the clients who uploaded those shares used our
                # TubID as a permutation-seed. We should keep using that same
                # seed to keep the shares in the same place in the permuted
                # ring, so those clients don't have to perform excessive
                # searches.
                seed = base32.b2a(self.nodeid)
            else:
                # otherwise, we're free to use the more natural seed of our
                # pubkey-based serverid
                vk_string = ed25519.string_from_verifying_key(self._node_public_key)
                vk_bytes = remove_prefix(vk_string, ed25519.PUBLIC_KEY_PREFIX)
                seed = base32.b2a(vk_bytes)
            self.config.write_config_file("permutation-seed", seed+"\n")
        return seed.strip()

    def init_storage(self):
        # should we run a storage server (and publish it for others to use)?
        if not self.config.get_config("storage", "enabled", True, boolean=True):
            return
        if not self._is_tub_listening():
            raise ValueError("config error: storage is enabled, but tub "
                             "is not listening ('tub.port=' is empty)")
        readonly = self.config.get_config("storage", "readonly", False, boolean=True)

        config_storedir = self.get_config(
            "storage", "storage_dir", self.STOREDIR,
        ).decode('utf-8')
        storedir = self.config.get_config_path(config_storedir)

        data = self.config.get_config("storage", "reserved_space", None)
        try:
            reserved = parse_abbreviated_size(data)
        except ValueError:
            log.msg("[storage]reserved_space= contains unparseable value %s"
                    % data)
            raise
        if reserved is None:
            reserved = 0
        discard = self.config.get_config("storage", "debug_discard", False,
                                         boolean=True)

        expire = self.config.get_config("storage", "expire.enabled", False, boolean=True)
        if expire:
            mode = self.config.get_config("storage", "expire.mode") # require a mode
        else:
            mode = self.config.get_config("storage", "expire.mode", "age")

        o_l_d = self.config.get_config("storage", "expire.override_lease_duration", None)
        if o_l_d is not None:
            o_l_d = parse_duration(o_l_d)

        cutoff_date = None
        if mode == "cutoff-date":
            cutoff_date = self.config.get_config("storage", "expire.cutoff_date")
            cutoff_date = parse_date(cutoff_date)

        sharetypes = []
        if self.config.get_config("storage", "expire.immutable", True, boolean=True):
            sharetypes.append("immutable")
        if self.config.get_config("storage", "expire.mutable", True, boolean=True):
            sharetypes.append("mutable")
        expiration_sharetypes = tuple(sharetypes)

        ss = StorageServer(storedir, self.nodeid,
                           reserved_space=reserved,
                           discard_storage=discard,
                           readonly_storage=readonly,
                           stats_provider=self.stats_provider,
                           expiration_enabled=expire,
                           expiration_mode=mode,
                           expiration_override_lease_duration=o_l_d,
                           expiration_cutoff_date=cutoff_date,
                           expiration_sharetypes=expiration_sharetypes)
        ss.setServiceParent(self)

        furl_file = self.config.get_private_path("storage.furl").encode(get_filesystem_encoding())
        furl = self.tub.registerReference(ss, furlFile=furl_file)
        ann = {"anonymous-storage-FURL": furl,
               "permutation-seed-base32": self._init_permutation_seed(ss),
               }
        for ic in self.introducer_clients:
            ic.publish("storage", ann, self._node_private_key)

    def init_client(self):
        helper_furl = self.config.get_config("client", "helper.furl", None)
        if helper_furl in ("None", ""):
            helper_furl = None

        DEP = self.encoding_params
        DEP["k"] = int(self.config.get_config("client", "shares.needed", DEP["k"]))
        DEP["n"] = int(self.config.get_config("client", "shares.total", DEP["n"]))
        DEP["happy"] = int(self.config.get_config("client", "shares.happy", DEP["happy"]))

        # for the CLI to authenticate to local JSON endpoints
        self._create_auth_token()

        self.history = History(self.stats_provider)
        self.terminator = Terminator()
        self.terminator.setServiceParent(self)
        uploader = Uploader(
            helper_furl,
            self.stats_provider,
            self.history,
        )
        uploader.setServiceParent(self)
        self.init_blacklist()
        self.init_nodemaker()

    def get_auth_token(self):
        """
        This returns a local authentication token, which is just some
        random data in "api_auth_token" which must be echoed to API
        calls.

        Currently only the URI '/magic' for magic-folder status; other
        endpoints are invited to include this as well, as appropriate.
        """
        return self.config.get_private_config('api_auth_token')

    def _create_auth_token(self):
        """
        Creates new auth-token data written to 'private/api_auth_token'.

        This is intentionally re-created every time the node starts.
        """
        self.config.write_private_config(
            'api_auth_token',
            urlsafe_b64encode(os.urandom(32)) + '\n',
        )

    def get_storage_broker(self):
        return self.storage_broker

    def load_static_servers(self):
        """
        Load the servers.yaml file if it exists, and provide the static
        server data to the StorageFarmBroker.
        """
        fn = self.config.get_private_path("servers.yaml")
        servers_filepath = FilePath(fn)
        try:
            with servers_filepath.open() as f:
                servers_yaml = yamlutil.safe_load(f)
            static_servers = servers_yaml.get("storage", {})
            log.msg("found %d static servers in private/servers.yaml" %
                    len(static_servers))
            self.storage_broker.set_static_servers(static_servers)
        except EnvironmentError:
            pass

    def init_blacklist(self):
        fn = self.config.get_config_path("access.blacklist")
        self.blacklist = Blacklist(fn)

    def init_nodemaker(self):
        default = self.config.get_config("client", "mutable.format", default="SDMF")
        if default.upper() == "MDMF":
            self.mutable_file_default = MDMF_VERSION
        else:
            self.mutable_file_default = SDMF_VERSION
        self.nodemaker = NodeMaker(self.storage_broker,
                                   self._secret_holder,
                                   self.get_history(),
                                   self.getServiceNamed("uploader"),
                                   self.terminator,
                                   self.get_encoding_parameters(),
                                   self.mutable_file_default,
                                   self._key_generator,
                                   self.blacklist)

    def get_history(self):
        return self.history

    def init_control(self):
        c = ControlServer()
        c.setServiceParent(self)
        control_url = self.control_tub.registerReference(c)
        self.config.write_private_config("control.furl", control_url + "\n")

    def init_helper(self):
        self.helper = Helper(self.config.get_config_path("helper"),
                             self.storage_broker, self._secret_holder,
                             self.stats_provider, self.history)
        # TODO: this is confusing. BASEDIR/private/helper.furl is created by
        # the helper. BASEDIR/helper.furl is consumed by the client who wants
        # to use the helper. I like having the filename be the same, since
        # that makes 'cp' work smoothly, but the difference between config
        # inputs and generated outputs is hard to see.
        helper_furlfile = self.config.get_private_path("helper.furl").encode(get_filesystem_encoding())
        self.tub.registerReference(self.helper, furlFile=helper_furlfile)

    def set_default_mutable_keysize(self, keysize):
        self._key_generator.set_default_keysize(keysize)

    def init_web(self, webport):
        self.log("init_web(webport=%s)", args=(webport,))

        from allmydata.webish import WebishServer
        nodeurl_path = self.config.get_config_path("node.url")
        staticdir_config = self.config.get_config("node", "web.static", "public_html").decode("utf-8")
        staticdir = self.config.get_config_path(staticdir_config)
        ws = WebishServer(self, webport, nodeurl_path, staticdir)
        ws.setServiceParent(self)

    def init_ftp_server(self):
        if self.config.get_config("ftpd", "enabled", False, boolean=True):
            accountfile = from_utf8_or_none(
                self.config.get_config("ftpd", "accounts.file", None))
            if accountfile:
                accountfile = self.config.get_config_path(accountfile)
            accounturl = self.config.get_config("ftpd", "accounts.url", None)
            ftp_portstr = self.config.get_config("ftpd", "port", "8021")

            from allmydata.frontends import ftpd
            s = ftpd.FTPServer(self, accountfile, accounturl, ftp_portstr)
            s.setServiceParent(self)

    def init_sftp_server(self):
        if self.config.get_config("sftpd", "enabled", False, boolean=True):
            accountfile = from_utf8_or_none(
                self.config.get_config("sftpd", "accounts.file", None))
            if accountfile:
                accountfile = self.config.get_config_path(accountfile)
            accounturl = self.config.get_config("sftpd", "accounts.url", None)
            sftp_portstr = self.config.get_config("sftpd", "port", "8022")
            pubkey_file = from_utf8_or_none(self.config.get_config("sftpd", "host_pubkey_file"))
            privkey_file = from_utf8_or_none(self.config.get_config("sftpd", "host_privkey_file"))

            from allmydata.frontends import sftpd
            s = sftpd.SFTPServer(self, accountfile, accounturl,
                                 sftp_portstr, pubkey_file, privkey_file)
            s.setServiceParent(self)

    def init_magic_folder(self):
        #print "init_magic_folder"
        if self.config.get_config("drop_upload", "enabled", False, boolean=True):
            raise node.OldConfigOptionError(
                "The [drop_upload] section must be renamed to [magic_folder].\n"
                "See docs/frontends/magic-folder.rst for more information."
            )

        if self.config.get_config("magic_folder", "enabled", False, boolean=True):
            from allmydata.frontends import magic_folder

            try:
                magic_folders = magic_folder.load_magic_folders(self.config._basedir)
            except Exception as e:
                log.msg("Error loading magic-folder config: {}".format(e))
                raise

            # start processing the upload queue when we've connected to
            # enough servers
            threshold = min(self.encoding_params["k"],
                            self.encoding_params["happy"] + 1)

            for (name, mf_config) in magic_folders.items():
                self.log("Starting magic_folder '{}'".format(name))
                s = magic_folder.MagicFolder.from_config(self, name, mf_config)
                self._magic_folders[name] = s
                s.setServiceParent(self)

                connected_d = self.storage_broker.when_connected_enough(threshold)
                def connected_enough(ign, mf):
                    mf.ready()  # returns a Deferred we ignore
                    return None
                connected_d.addCallback(connected_enough, s)

    def _check_exit_trigger(self, exit_trigger_file):
        if os.path.exists(exit_trigger_file):
            mtime = os.stat(exit_trigger_file)[stat.ST_MTIME]
            if mtime > time.time() - 120.0:
                return
            else:
                self.log("%s file too old, shutting down" % (self.EXIT_TRIGGER_FILE,))
        else:
            self.log("%s file missing, shutting down" % (self.EXIT_TRIGGER_FILE,))
        reactor.stop()

    def get_encoding_parameters(self):
        return self.encoding_params

    def introducer_connection_statuses(self):
        return [ic.connection_status() for ic in self.introducer_clients]

    def connected_to_introducer(self):
        return any([ic.connected_to_introducer() for ic in self.introducer_clients])

    def get_renewal_secret(self): # this will go away
        return self._secret_holder.get_renewal_secret()

    def get_cancel_secret(self):
        return self._secret_holder.get_cancel_secret()

    def debug_wait_for_client_connections(self, num_clients):
        """Return a Deferred that fires (with None) when we have connections
        to the given number of peers. Useful for tests that set up a
        temporary test network and need to know when it is safe to proceed
        with an upload or download."""
        def _check():
            return len(self.storage_broker.get_connected_servers()) >= num_clients
        d = self.poll(_check, 0.5)
        d.addCallback(lambda res: None)
        return d


    # these four methods are the primitives for creating filenodes and
    # dirnodes. The first takes a URI and produces a filenode or (new-style)
    # dirnode. The other three create brand-new filenodes/dirnodes.

    def create_node_from_uri(self, write_uri, read_uri=None, deep_immutable=False, name="<unknown name>"):
        # This returns synchronously.
        # Note that it does *not* validate the write_uri and read_uri; instead we
        # may get an opaque node if there were any problems.
        return self.nodemaker.create_from_cap(write_uri, read_uri, deep_immutable=deep_immutable, name=name)

    def create_dirnode(self, initial_children={}, version=None):
        d = self.nodemaker.create_new_mutable_directory(initial_children, version=version)
        return d

    def create_immutable_dirnode(self, children, convergence=None):
        return self.nodemaker.create_immutable_directory(children, convergence)

    def create_mutable_file(self, contents=None, keysize=None, version=None):
        return self.nodemaker.create_mutable_file(contents, keysize,
                                                  version=version)

    def upload(self, uploadable, reactor=None):
        uploader = self.getServiceNamed("uploader")
        return uploader.upload(uploadable, reactor=reactor)
