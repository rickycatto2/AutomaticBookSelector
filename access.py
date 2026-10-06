"""Validate Cloudflare Access identities before serving a configured public host."""
import os
import re
import threading
import urllib.parse


class AccessPolicy:
    def __init__(self, port, public_origin='', team_domain='', audience='', emails=''):
        self.local_hosts = {f'localhost:{port}', f'127.0.0.1:{port}'}
        self.local_origins = {'http://' + h for h in self.local_hosts}
        self.public_origin = public_origin.rstrip('/')
        self.team_domain = team_domain.rstrip('/')
        self.audience = audience
        self.emails = {e.strip().lower() for e in emails.split(',') if e.strip()}
        self.public_host = ''
        self.jwks = None
        self.lock = threading.Lock()
        if self.public_origin:
            parsed = urllib.parse.urlsplit(self.public_origin)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or parsed.port not in (None, 443):
                raise ValueError('APP_PUBLIC_ORIGIN must be an HTTPS origin without a path or credentials.')
            self.public_host = parsed.netloc.lower()
            if parsed.hostname.lower() in ('localhost', '127.0.0.1', '::1'):
                raise ValueError('APP_PUBLIC_ORIGIN must use a public hostname.')
        if self.team_domain and not re.fullmatch(r'https://[a-z0-9-]+\.cloudflareaccess\.com', self.team_domain):
            raise ValueError('CF_ACCESS_TEAM_DOMAIN must be your HTTPS cloudflareaccess.com team URL.')

    @classmethod
    def from_env(cls, port):
        return cls(port, os.getenv('APP_PUBLIC_ORIGIN', ''), os.getenv('CF_ACCESS_TEAM_DOMAIN', ''),
                   os.getenv('CF_ACCESS_AUD', ''), os.getenv('CF_ACCESS_EMAILS', ''))

    @property
    def configured(self):
        return bool(self.public_host and self.team_domain and self.audience and self.emails)

    def host_kind(self, host):
        host = (host or '').lower()
        if host in self.local_hosts:
            return 'local'
        if self.public_host and host == self.public_host:
            return 'remote'
        return None

    def verify(self, token):
        if not self.configured or not token or len(token) > 16384:
            return False
        try:
            import jwt
            with self.lock:
                if self.jwks is None:
                    self.jwks = jwt.PyJWKClient(self.team_domain + '/cdn-cgi/access/certs', timeout=5,
                                               cache_keys=True, cache_jwk_set=True, lifespan=300)
                key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=['RS256'], audience=self.audience, issuer=self.team_domain,
                                options={'require': ['exp', 'iat', 'iss', 'aud', 'email', 'sub']})
            return isinstance(claims.get('email'), str) and claims['email'].lower() in self.emails
        except Exception:
            # No header-only identity trust, token logging, or permissive fallback.
            return False

    def authorize(self, headers):
        kind = self.host_kind(headers.get('Host'))
        return kind == 'local' or (kind == 'remote' and self.verify(headers.get('Cf-Access-Jwt-Assertion')))

    def valid_origin(self, headers):
        origin = headers.get('Origin')
        kind = self.host_kind(headers.get('Host'))
        if kind == 'remote':
            return origin == self.public_origin
        return kind == 'local' and (not origin or origin in self.local_origins)
