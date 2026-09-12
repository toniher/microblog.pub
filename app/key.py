from pathlib import Path

from Crypto.PublicKey import RSA


def generate_key(key_path: Path) -> None:
    if key_path.exists():
        raise ValueError(f"Key at {key_path} already exists")
    k = RSA.generate(2048)
    privkey_pem = k.exportKey("PEM").decode("utf-8")
    key_path.write_text(privkey_pem)


def get_pubkey_as_pem(key_path: Path) -> str:
    text = key_path.read_text()
    return RSA.import_key(text).public_key().export_key("PEM").decode("utf-8")


class Key(object):
    DEFAULT_KEY_SIZE = 2048

    def __init__(self, owner: str, id_: str | None = None) -> None:
        self.owner = owner
        self.privkey_pem: str | None = None
        self.pubkey_pem: str | None = None
        self.privkey: RSA.RsaKey | None = None
        self.pubkey: RSA.RsaKey | None = None
        self.id_ = id_

    def load_pub(self, pubkey_pem: str) -> None:
        self.pubkey_pem = pubkey_pem
        self.pubkey = RSA.importKey(pubkey_pem)

    def load(self, privkey_pem: str) -> None:
        self.privkey_pem = privkey_pem
        self.privkey = RSA.importKey(self.privkey_pem)
        self.pubkey_pem = self.privkey.publickey().exportKey("PEM").decode("utf-8")

    def new(self) -> None:
        k = RSA.generate(self.DEFAULT_KEY_SIZE)
        self.privkey_pem = k.exportKey("PEM").decode("utf-8")
        self.pubkey_pem = k.publickey().exportKey("PEM").decode("utf-8")
        self.privkey = k

    def key_id(self) -> str:
        return self.id_ or f"{self.owner}#main-key"
