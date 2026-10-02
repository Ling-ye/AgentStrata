"""Pure iLink media encryption and strict decryption."""

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from chatcopilot.contracts.weixin import WeixinError


def encrypt(data: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return cipher.update(padded) + cipher.finalize()


def decrypt(data: bytes, key: bytes) -> bytes:
    try:
        cipher = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = cipher.update(data) + cipher.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise WeixinError("weixin_media_decryption_failed") from None
