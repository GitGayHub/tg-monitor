import sys
import os
import hashlib
import hmac

# Формат нового шифротекста: MAGIC || salt(16) || tag(32) || ciphertext.
# Старый формат (salt(16) || ciphertext) читается для совместимости с уже
# зашифрованными файлами, но новые записи всегда идут в новом формате.
MAGIC = b'TGMC1\x00'
SALT_LEN = 16
TAG_LEN = 32
PBKDF2_ROUNDS = 200_000


def get_keystream(key, length):
    keystream = bytearray()
    counter = 0
    while len(keystream) < length:
        ctx = hashlib.sha256()
        ctx.update(key)
        ctx.update(counter.to_bytes(4, 'big'))
        keystream.extend(ctx.digest())
        counter += 1
    return keystream[:length]


def _derive_keys(passphrase: str, salt: bytes):
    """Возвращает (ключ шифрования, ключ подписи)."""
    material = hashlib.pbkdf2_hmac('sha256', passphrase.encode('utf-8'), salt, PBKDF2_ROUNDS, dklen=64)
    return material[:32], material[32:]


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, get_keystream(key, len(data))))


def encrypt(data: bytes, passphrase: str) -> bytes:
    salt = os.urandom(SALT_LEN)
    enc_key, mac_key = _derive_keys(passphrase, salt)
    ciphertext = _xor(data, enc_key)
    tag = hmac.new(mac_key, salt + ciphertext, hashlib.sha256).digest()
    return MAGIC + salt + tag + ciphertext


def decrypt(data: bytes, passphrase: str) -> bytes:
    if data.startswith(MAGIC):
        body = data[len(MAGIC):]
        if len(body) < SALT_LEN + TAG_LEN:
            raise ValueError("Data is too short to be valid ciphertext")
        salt = body[:SALT_LEN]
        tag = body[SALT_LEN:SALT_LEN + TAG_LEN]
        ciphertext = body[SALT_LEN + TAG_LEN:]
        enc_key, mac_key = _derive_keys(passphrase, salt)
        expected = hmac.new(mac_key, salt + ciphertext, hashlib.sha256).digest()
        # Без этой проверки неверный пароль возвращал бы мусор, который затем
        # затирал рабочий config.json и уезжал в таком виде на GitHub.
        if not hmac.compare_digest(tag, expected):
            raise ValueError("Неверный CONFIG_PASSPHRASE: не сходится контрольная сумма")
        return _xor(ciphertext, enc_key)

    # Наследуемый формат без подписи.
    if len(data) < SALT_LEN:
        raise ValueError("Data is too short to be valid ciphertext")
    salt = data[:SALT_LEN]
    ciphertext = data[SALT_LEN:]
    key = hashlib.sha256(passphrase.encode('utf-8') + salt).digest()
    plaintext = _xor(ciphertext, key)
    # Подписи здесь нет, поэтому единственная доступная проверка — что результат
    # вообще является текстом. При неверном пароле байты почти всегда не UTF-8.
    try:
        plaintext.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError("Неверный CONFIG_PASSPHRASE: расшифрованные данные не являются текстом")
    return plaintext


def encrypt_to_file(data: bytes, passphrase: str, output_path: str) -> bool:
    """Шифрует и записывает файл ТОЛЬКО если содержимое реально изменилось.

    Соль случайна, поэтому повторное шифрование того же конфига даёт полностью
    другой шифротекст — дельта-сжатие невозможно. Безусловная перезапись при
    каждом запуске бота добавляла в git по 23 КБ несжимаемых данных: на историю
    репозитория это дало 163 МБ из 204 при неизменных настройках.

    Возвращает True, если файл был перезаписан.
    """
    if os.path.exists(output_path):
        try:
            with open(output_path, "rb") as f:
                existing = f.read()
            if decrypt(existing, passphrase) == data:
                return False
        except Exception:
            pass  # не расшифровалось (другой пароль, пустой файл) — перезапишем
    tmp_path = output_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(encrypt(data, passphrase))
    os.replace(tmp_path, output_path)
    return True


def main():
    args = sys.argv[1:]
    if len(args) == 4:
        action, input_file, output_file, passphrase = args
    elif len(args) == 3:
        action, input_file, output_file = args
        passphrase = os.environ.get("CONFIG_PASSPHRASE")
        if not passphrase:
            print("Error: CONFIG_PASSPHRASE is not set.")
            sys.exit(1)
    else:
        print("Usage: python config_crypt.py <encrypt|decrypt> <input_file> <output_file> [passphrase]")
        print("Or set CONFIG_PASSPHRASE environment variable.")
        sys.exit(1)

    if action not in ("encrypt", "decrypt"):
        print(f"Error: Unknown action '{action}' (must be 'encrypt' or 'decrypt')")
        sys.exit(1)

    if not os.path.exists(input_file):
        print(f"Error: Input file '{input_file}' not found.")
        sys.exit(1)

    try:
        with open(input_file, "rb") as f:
            data = f.read()

        if action == "encrypt":
            # Проверяем, существует ли уже зашифрованный файл и равен ли его расшифрованный контент новому
            if os.path.exists(output_file):
                try:
                    with open(output_file, "rb") as f_out:
                        existing_encrypted = f_out.read()
                    existing_decrypted = decrypt(existing_encrypted, passphrase)
                    if existing_decrypted == data:
                        print(f"No changes: '{output_file}' decrypted content is identical to '{input_file}'. Skipping write.")
                        sys.exit(0)
                except Exception:
                    # Если расшифровать не удалось (например, неверный пароль или пустой файл), просто перезапишем
                    pass
            out_data = encrypt(data, passphrase)
        else:
            out_data = decrypt(data, passphrase)

        with open(output_file, "wb") as f:
            f.write(out_data)

        print(f"Success: {action}ed '{input_file}' to '{output_file}'.")
    except Exception as e:
        print(f"Error during {action}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
