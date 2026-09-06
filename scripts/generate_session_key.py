"""Print a Fernet-compatible Telegram session encryption key."""

from cryptography.fernet import Fernet


if __name__ == "__main__":
    print(Fernet.generate_key().decode("ascii"))
