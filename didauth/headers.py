import base64
import os
from typing import Mapping, Sequence

import multidict

from .base import KeyFinderBase, SignerBase
from . import registry
from .utils import \
    build_signature_template, generate_message, parse_authorization_header, \
    decode_string, encode_string, HttpSigException


class HeaderSigner:
    """
    Generic object that will sign headers as a dictionary using the
        http-signature scheme.
    https://github.com/joyent/node-http-signature/blob/master/http_signing.md

    :arg key_id:       the mandatory label indicating to the server which secret
        to use
    :arg signer:       the BaseSigner instance implementing the signing algorithm
    :arg header_list:  a list of http headers to be included in the signing
        string, defaulting to ['date'].
    """
    def __init__(self, key_id: str, signer: SignerBase, header_list=None):
        self._key_id = key_id
        self._header_list = None
        self._signer = signer
        self._signature_tpl = ''
        self.header_list = header_list

    @property
    def header_list(self):
        return self._header_list

    @header_list.setter
    def header_list(self, val: Sequence):
        if val:
            self._header_list = list(header.lower() for header in val)
            # consistent order to headers for verification during testing
            self._header_list.sort()
        else:
            self._header_list = ['date']
        self._signature_tpl = build_signature_template(
            self._key_id,
            self._signer.algorithm,
            self._header_list)

    @property
    def key_id(self):
        return self._key_id

    @property
    def signer(self):
        return self._signer

    def sign(self, headers: Mapping, host=None, method=None, path=None):
        """
        Add Signature Authorization header to case-insensitive header dict.

        `headers` is a case-insensitive dict of mutable headers.
        `host` is a override for the 'host' header (defaults to value in
            headers).
        `method` is the HTTP method (required when using '(request-target)').
        `path` is the HTTP path (required when using '(request-target)').
        """
        required_headers = self.header_list
        message = generate_message(required_headers, headers, host, method, path)

        signature = encode_string(self._signer.sign(message), 'base64')
        ret_headers = multidict.CIMultiDict(headers)
        ret_headers['Authorization'] = self._signature_tpl % signature.decode('ascii')

        return ret_headers


class HeaderVerifier:
    """
    Verifies an HTTP signature from given headers.
    """
    def __init__(self, key_finder:KeyFinderBase, handlers:registry.SignatureHandlers=None,
                 required_headers=None):
        self._key_finder = key_finder
        self._handlers = handlers or registry.ALL
        if required_headers is None:
            required_headers = [] # implementors should require (request-target) and date
        self._required_headers = [h.lower() for h in required_headers]

    def verify(self, headers: Mapping, host=None, method=None, path=None):
        """
        Parse Signature Authorization header and verify signature

        `headers` is a dict or multidict of headers
        `host` is a override for the 'host' header (defaults to value in
            headers).
        `method` is the HTTP method (required when using '(request-target)').
        `path` is the HTTP path (required when using '(request-target)').
        """

        if not 'authorization' in headers:
            return False

        auth_type, auth_params = parse_authorization_header(headers['authorization'])
        if auth_type.lower() != 'signature':
            return False

        for param in ('algorithm', 'keyId', 'signature'):
            if param not in auth_params:
                raise HttpSigException("Unsupported HTTP signature, missing '{}'".format(param))

        auth_headers = (auth_params.get('headers') or 'date').lower().strip().split()

        missing_reqd = set(self._required_headers) - set(auth_headers)
        if len(missing_reqd) > 0:
            error_headers = ', '.join(missing_reqd)
            raise HttpSigException(
                    'One or more required headers not provided: {}'.format(missing_reqd))

        key_id, algo = auth_params['keyId'], auth_params['algorithm']

        if not self._handlers.supports(algo):
            raise HttpSigException("Unsupported HTTP signature algorithm '{}'".format(algo))

        pubkey = self._key_finder.find_key(key_id, algo)
        if not pubkey:
            raise HttpSigException("Cannot locate public key for '{}'".format(key_id))

        handler = self._handlers.create_verifier(algo, pubkey)
        message = generate_message(auth_headers, headers, host, method, path)

        signature = decode_string(auth_params['signature'], 'base64')

        return handler.verify(message, signature)
