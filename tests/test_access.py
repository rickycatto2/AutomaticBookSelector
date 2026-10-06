import time
import types
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from access import AccessPolicy


class FixedJWKS:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, token):
        return types.SimpleNamespace(key=self.key)


class AccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.policy = AccessPolicy(5077, 'https://selector.example.com', 'https://reader.cloudflareaccess.com', 'app-audience', 'reader@example.com')
        self.policy.jwks = FixedJWKS(self.private.public_key())

    def token(self, **changes):
        claims = {'iss': self.policy.team_domain, 'aud': ['app-audience'], 'exp': int(time.time()) + 60,
                  'iat': int(time.time()) - 1, 'email': 'reader@example.com', 'sub': 'reader-id'}
        claims.update(changes)
        return jwt.encode(claims, self.private, algorithm='RS256')

    def test_valid_identity_and_local_requests(self):
        self.assertTrue(self.policy.verify(self.token()))
        self.assertTrue(self.policy.authorize({'Host': 'localhost:5077'}))
        self.assertTrue(self.policy.authorize({'Host': 'selector.example.com', 'Cf-Access-Jwt-Assertion': self.token()}))

    def test_wrong_email_audience_issuer_or_expiry_are_rejected(self):
        for claims in ({'email': 'someone@example.com'}, {'aud': ['another-app']}, {'iss': 'https://evil.example'}, {'exp': int(time.time()) - 60}):
            self.assertFalse(self.policy.verify(self.token(**claims)))

    def test_unsigned_and_tampered_tokens_are_rejected(self):
        token = jwt.encode({'email': 'reader@example.com'}, key=None, algorithm='none')
        self.assertFalse(self.policy.verify(token))
        claims = jwt.decode(self.token(), options={'verify_signature': False})
        self.assertFalse(self.policy.verify(jwt.encode(claims, self.other_private, algorithm='RS256')))
        self.assertFalse(self.policy.verify('invalid'))

    def test_email_header_without_signed_identity_never_authorizes(self):
        self.assertFalse(self.policy.authorize({'Host': 'selector.example.com', 'Cf-Access-Authenticated-User-Email': 'reader@example.com'}))

    def test_remote_write_requires_exact_https_origin(self):
        self.assertTrue(self.policy.valid_origin({'Host': 'selector.example.com', 'Origin': 'https://selector.example.com'}))
        for origin in (None, 'http://selector.example.com', 'https://evil.example', 'http://localhost:5077'):
            self.assertFalse(self.policy.valid_origin({'Host': 'selector.example.com', 'Origin': origin}))

    def test_incomplete_configuration_denies_remote_access(self):
        policy = AccessPolicy(5077, public_origin='https://selector.example.com')
        self.assertFalse(policy.authorize({'Host': 'selector.example.com', 'Cf-Access-Jwt-Assertion': self.token()}))
        self.assertTrue(policy.authorize({'Host': 'localhost:5077'}))

    def test_configuration_rejects_untrusted_issuer_or_bad_origin(self):
        for value in ('http://selector.example.com', 'https://selector.example.com/path', 'https://user:secret@selector.example.com', 'https://localhost'):
            with self.assertRaises(ValueError):
                AccessPolicy(5077, public_origin=value)
        with self.assertRaises(ValueError):
            AccessPolicy(5077, team_domain='https://attacker.example')


if __name__ == '__main__':
    unittest.main()
