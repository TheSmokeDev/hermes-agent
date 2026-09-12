"""Profile-scoped worker registry using the existing plugin registration lifecycle."""
import logging

from agent.provider_registry import ProviderRegistry
from agent.task_worker_provider import TaskWorkerProvider
from hermes_constants import hermes_home_key

_registry = ProviderRegistry(label="Task worker", provider_cls=TaskWorkerProvider,
                             logger=logging.getLogger(__name__))
_registry.export(globals())


def configured_worker(name):
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    provider = _registry.snapshot_registration(name, scope=hermes_home_key())
    return provider if provider is not None and provider.available() is True else None


def available_workers():
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
    scope = hermes_home_key()
    return [provider.name for provider in _registry.list_providers(scope=scope)
            if _registry.snapshot_registration(provider.name, scope=scope) is provider
            and provider.available() is True]
