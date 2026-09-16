from __future__ import annotations

import os


class AICredentialError(RuntimeError):
    pass


class AICredentialService:
    SERVICE_NAME = "ResearchAssistant"
    _LEGACY_PROVIDER_NAMES = {"custom": ("vilao", "api" + "box")}
    ENVIRONMENT_KEYS = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "claude": "ANTHROPIC_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "custom": "CUSTOM_API_KEY",
    }
    _LEGACY_ENVIRONMENT_KEYS = {"custom": ("VILAO_API_KEY",)}

    @classmethod
    def _username(cls, provider: str) -> str:
        return f"ai:{provider.strip().lower()}"

    @classmethod
    def migrate_legacy_custom_credential(cls) -> None:
        """Move a legacy compatible-provider key into the generic vault slot."""
        cls.get_stored_api_key("custom")

    @classmethod
    def get_api_key(cls, provider: str) -> str:
        name = provider.strip().lower()
        stored = cls.get_stored_api_key(name)
        if stored:
            return stored
        configured = os.environ.get(cls.ENVIRONMENT_KEYS.get(name, ""), "").strip()
        if configured:
            return configured
        for environment_name in cls._LEGACY_ENVIRONMENT_KEYS.get(name, ()):
            value = os.environ.get(environment_name, "").strip()
            if value:
                return value
        return ""

    @classmethod
    def get_stored_api_key(cls, provider: str) -> str:
        name = provider.strip().lower()
        try:
            import keyring

            stored = keyring.get_password(cls.SERVICE_NAME, cls._username(name))
            if not stored and name in cls._LEGACY_PROVIDER_NAMES:
                for legacy_name in cls._LEGACY_PROVIDER_NAMES[name]:
                    stored = keyring.get_password(
                        cls.SERVICE_NAME,
                        cls._username(legacy_name),
                    )
                    if not stored:
                        continue
                    # Move a legacy vendor credential into the generic slot
                    # once, without putting its value in SQLite or UI state.
                    try:
                        keyring.set_password(
                            cls.SERVICE_NAME,
                            cls._username(name),
                            stored,
                        )
                        keyring.delete_password(
                            cls.SERVICE_NAME,
                            cls._username(legacy_name),
                        )
                    except Exception:
                        pass
                    break
        except Exception:
            stored = None
        return stored.strip() if stored else ""

    @classmethod
    def has_stored_api_key(cls, provider: str) -> bool:
        return bool(cls.get_stored_api_key(provider))

    @classmethod
    def has_environment_api_key(cls, provider: str) -> bool:
        name = provider.strip().lower()
        return bool(
            os.environ.get(cls.ENVIRONMENT_KEYS.get(name, ""), "").strip()
            or any(
                os.environ.get(environment_name, "").strip()
                for environment_name in cls._LEGACY_ENVIRONMENT_KEYS.get(name, ())
            )
        )

    @classmethod
    def has_api_key(cls, provider: str) -> bool:
        return bool(cls.get_api_key(provider))

    @classmethod
    def set_api_key(cls, provider: str, value: str) -> None:
        secret = value.strip()
        if not secret:
            return
        try:
            import keyring

            keyring.set_password(
                cls.SERVICE_NAME,
                cls._username(provider),
                secret,
            )
        except Exception as error:
            raise AICredentialError(
                "Could not store the API key in the operating-system credential vault."
            ) from error

    @classmethod
    def delete_api_key(cls, provider: str) -> None:
        """Delete only the credential stored by ResearchAssistant for a provider."""
        name = provider.strip().lower()
        try:
            import keyring

            if not keyring.get_password(cls.SERVICE_NAME, cls._username(name)):
                return
            keyring.delete_password(cls.SERVICE_NAME, cls._username(name))
        except Exception as error:
            try:
                from keyring.errors import PasswordDeleteError
            except ImportError:
                PasswordDeleteError = ()
            if PasswordDeleteError and isinstance(error, PasswordDeleteError):
                return
            raise AICredentialError(
                "Could not delete the API key from the operating-system credential vault."
            ) from error
