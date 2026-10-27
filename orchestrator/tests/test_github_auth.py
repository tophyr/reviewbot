import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from reviewbot import github_auth
from reviewbot.github_auth import AppAuth, make_app_jwt, parse_expires_at

NOW = 1_800_000_000


def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    return pem, pub


PEM, PUB = keypair()


def test_jwt_claims_and_signature():
    token = make_app_jwt(123456, PEM, now=NOW)
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(token, PUB, algorithms=["RS256"], options={"verify_exp": False,
                                                                    "verify_iat": False})
    assert claims == {"iat": NOW - 60, "exp": NOW - 60 + 600, "iss": "123456"}
    # GitHub: exp no more than 10 minutes into the future
    assert claims["exp"] - NOW <= 600


def test_jwt_valid_now_with_real_clock():
    token = make_app_jwt("42", PEM)
    claims = jwt.decode(token, PUB, algorithms=["RS256"], issuer="42")
    assert claims["iss"] == "42"


def test_jwt_rejected_by_other_key():
    _, other_pub = keypair()
    token = make_app_jwt("42", PEM)
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, other_pub, algorithms=["RS256"])


def test_parse_expires_at():
    assert parse_expires_at("2016-07-11T22:14:10Z", 0) == 1468275250.0
    assert parse_expires_at("garbage", 100.0) == 100.0 + github_auth.FALLBACK_TOKEN_LIFETIME


class FakeResp:
    def __init__(self, data, status=201):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data


class FakeAPI:
    def __init__(self, clock):
        self.clock = clock
        self.posts = []
        self.n = 0

    def request(self, method, path, token, json=None, expected=(200,), **kw):
        assert method == "POST"
        # The bearer must be a JWT signed by the app key.
        jwt.decode(token, PUB, algorithms=["RS256"], options={"verify_exp": False,
                                                                "verify_iat": False})
        self.posts.append((path, json))
        self.n += 1
        exp = self.clock.t + 3600
        from datetime import datetime, timezone
        return FakeResp({"token": f"ghs_fake{self.n:032d}",
                         "expires_at": datetime.fromtimestamp(exp, timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ")})

    def get_json(self, path, token, params=None):
        assert path == "/repos/o/r/installation"
        return {"id": 77}


class Clock:
    def __init__(self):
        self.t = float(NOW)

    def __call__(self):
        return self.t


def test_installation_token_cached_until_five_minutes_before_expiry():
    clock = Clock()
    api = FakeAPI(clock)
    auth = AppAuth(api, "1", PEM, clock=clock)
    t1 = auth.installation_token(5)
    assert api.posts == [("/app/installations/5/access_tokens", None)]
    clock.t += 3600 - 301
    assert auth.installation_token(5) == t1
    assert len(api.posts) == 1
    clock.t += 2  # now inside the 5 minute margin
    t2 = auth.installation_token(5)
    assert t2 != t1 and len(api.posts) == 2
    # a different installation has its own token
    auth.installation_token(6)
    assert len(api.posts) == 3


def test_fetch_token_is_scoped_and_not_cached():
    clock = Clock()
    api = FakeAPI(clock)
    auth = AppAuth(api, "1", PEM, clock=clock)
    a = auth.fetch_token_for_repo("o/r")
    b = auth.fetch_token_for_repo("o/r")
    assert a != b
    assert api.posts == [
        ("/app/installations/77/access_tokens",
         {"repositories": ["r"], "permissions": {"contents": "read", "pull_requests": "read"}}),
    ] * 2
