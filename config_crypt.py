import sys
import os
import hashlib

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

def encrypt(data: bytes, passphrase: str) -> bytes:
    salt = os.urandom(16)
    key = hashlib.sha256(passphrase.encode('utf-8') + salt).digest()
    keystream = get_keystream(key, len(data))
    ciphertext = bytes(a ^ b for a, b in zip(data, keystream))
    return salt + ciphertext

def decrypt(data: bytes, passphrase: str) -> bytes:
    if len(data) < 16:
        raise ValueError("Data is too short to be valid ciphertext")
    salt = data[:16]
    ciphertext = data[16:]
    key = hashlib.sha256(passphrase.encode('utf-8') + salt).digest()
    keystream = get_keystream(key, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, keystream))

def main():
    if len(sys.argv) < 4:
        # Check if environment variable is set
        passphrase = os.environ.get("CONFIG_PASSPHRASE")
        if not passphrase:
            print("Usage: python config_crypt.py <encrypt|decrypt> <input_file> <output_file> [passphrase]")
            print("Or set CONFIG_PASSPHRASE environment variable.")
            sys.exit(1)
        action = sys.argv[1]
        input_file = sys.argv[2]
        output_file = sys.argv[3]
    else:
        action = sys.argv[1]
        input_file = sys.argv[2]
        output_file = sys.argv[3]
        passphrase = sys.argv[4]

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
