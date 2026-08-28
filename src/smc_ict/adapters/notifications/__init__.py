"""Concrete notification transports."""

from .generic_webhook import GenericWebhookNotifier

__all__ = ["GenericWebhookNotifier"]
