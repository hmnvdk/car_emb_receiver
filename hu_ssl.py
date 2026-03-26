"""
SSL layer — port of gartnera/headunit hu_ssl.cpp

HU acts as TLS CLIENT (set_connect_state).
Phone is TLS server.  In-band handshake via MSG_SSL_HANDSHAKE frames.

pyOpenSSL Memory-BIO pattern:
  conn = Connection(ctx, None)   # None → use memory BIOs
  conn.bio_write(incoming_tls)   # feed bytes from network into SSL
  conn.do_handshake()            # or conn.read() for app data
  outgoing = conn.bio_read(N)    # drain bytes that must go to network
"""
import logging
import os

from OpenSSL.SSL import (
    Context, Connection,
    TLSv1_2_METHOD,
    OP_NO_SSLv2, OP_NO_SSLv3,
    VERIFY_PEER,
    WantReadError, ZeroReturnError, Error as SSLError,
)

log = logging.getLogger(__name__)

CERT_DIR = os.path.join(os.path.dirname(__file__), "certs")


class HUSSLLayer:
    """
    Memory-BIO TLS session (pyOpenSSL).
    Mirrors gartnera hu_ssl.cpp:
      hu_ssl_rm_bio  → conn.bio_write()   (network → SSL)
      hu_ssl_wm_bio  → conn.bio_read()    (SSL → network)
      SSL_read/write → conn.read/write()
    """

    def __init__(self, cert_type: str = "jaguar"):
        self._ready     = False
        self._cert_type = cert_type
        self._conn      = self._make_connection(cert_type)

    # ── Public ─────────────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._ready

    def begin_handshake(self) -> bytes:
        """Set TLS client mode and produce ClientHello bytes."""
        self._conn.set_connect_state()
        try:
            self._conn.do_handshake()
        except WantReadError:
            pass
        return self._drain()

    def feed(self, data: bytes) -> bytes:
        """Feed incoming TLS bytes; return outgoing TLS bytes (if any).

        When handshake completes, is_ready becomes True.
        """
        self._conn.bio_write(data)
        if not self._ready:
            try:
                self._conn.do_handshake()
                self._ready = True
                log.info("TLS handshake завершён (client, cert=%s).", self._cert_type)
            except WantReadError:
                pass
            except SSLError as e:
                log.error("TLS handshake error: %s", e)
                raise
        return self._drain()

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt plaintext; return TLS record bytes."""
        self._conn.write(plaintext)
        return self._drain()

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt TLS record bytes; return plaintext."""
        self._conn.bio_write(ciphertext)
        out = bytearray()
        while True:
            try:
                chunk = self._conn.read(65536)
                if chunk:
                    out.extend(chunk)
                else:
                    break
            except ZeroReturnError:
                break
            except WantReadError:
                break
        return bytes(out)

    # ── Internals ──────────────────────────────────────────────────────────

    def _drain(self) -> bytes:
        """Drain pending outgoing TLS bytes from the write BIO."""
        out = bytearray()
        while True:
            try:
                chunk = self._conn.bio_read(4096)
                if not chunk:
                    break
                out.extend(chunk)
            except Exception:
                break
        return bytes(out)

    def _make_connection(self, cert_type: str) -> Connection:
        ctx = Context(TLSv1_2_METHOD)
        ctx.set_options(OP_NO_SSLv2 | OP_NO_SSLv3)

        if cert_type == "lr":
            crt   = os.path.join(CERT_DIR, "lr.crt")
            key   = os.path.join(CERT_DIR, "lr.key")
            chain = os.path.join(CERT_DIR, "lr_chain.crt")
            log.info("SSL: сертификат lr (Land Rover OEM)")
        else:
            crt   = os.path.join(CERT_DIR, "jaguar.crt")
            key   = os.path.join(CERT_DIR, "jaguar.key")
            chain = os.path.join(CERT_DIR, "jaguar_chain.crt")
            log.info("SSL: сертификат jaguar (Jaguar Land Rover OEM)")

        ctx.use_certificate_file(crt)
        ctx.use_privatekey_file(key)
        if os.path.exists(chain):
            ctx.load_verify_locations(chain)

        ca = os.path.join(CERT_DIR, "google_aa_ca.crt")
        if os.path.exists(ca):
            ctx.load_verify_locations(ca)
            ctx.set_verify(VERIFY_PEER, lambda conn, cert, errnum, depth, ok: True)

        conn = Connection(ctx, None)   # None = memory BIOs (gartnera style)
        return conn
