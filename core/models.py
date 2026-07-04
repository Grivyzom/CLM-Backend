"""
Core models for CLM System configuration.
"""

from django.db import models


class SystemConfig(models.Model):
    """
    Global system configuration (singleton pattern).
    Stores default currency and other system-wide settings.
    """

    CURRENCY_CHOICES = [
        ("CLP", "Pesos Chilenos"),
        ("USD", "Dólares"),
        ("EUR", "Euros"),
    ]

    # Currency configuration
    default_currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default="CLP",
        verbose_name="Moneda por defecto",
        help_text="Moneda utilizada por defecto en el sistema (CLP, USD, EUR)",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Configuración del Sistema"
        verbose_name_plural = "Configuración del Sistema"

    def __str__(self):
        return f"Sistema - Moneda: {self.default_currency}"

    @classmethod
    def get_config(cls):
        """
        Get or create the system configuration (singleton).
        Always returns a single SystemConfig instance.
        """
        config, created = cls.objects.get_or_create(pk=1)
        return config

    @classmethod
    def get_default_currency(cls):
        """
        Get the default currency configured in the system.
        """
        config = cls.get_config()
        return config.default_currency

    @classmethod
    def set_default_currency(cls, currency_code):
        """
        Update the default currency.

        Args:
            currency_code (str): Currency code (CLP, USD, EUR)

        Raises:
            ValueError: If currency_code is not valid
        """
        valid_currencies = [choice[0] for choice in cls.CURRENCY_CHOICES]
        if currency_code not in valid_currencies:
            raise ValueError(
                f"Moneda inválida: {currency_code}. Soportadas: {valid_currencies}"
            )

        config = cls.get_config()
        config.default_currency = currency_code
        config.save()
        return config
